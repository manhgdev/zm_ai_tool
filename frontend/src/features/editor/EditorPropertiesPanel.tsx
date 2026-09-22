/**
 * Panel Thuộc tính (tab Phụ đề / Video / Âm thanh / Vùng che chữ / Overlay)
 * — tách nguyên văn từ LivePreviewEditor; state + handler vẫn ở component cha.
 */
import React from 'react'
import type { ProjectSettings, Segment, TextOverlay } from '@/features/project/project.types'
import { resolvedSpeakerProfiles, speakerRoleOptions } from '@/features/project/speakerProfiles'
import { localize, useLocale } from '@/app/i18n'
import { normalizeTranslatorForEngine, translatorOptions } from '@/app/appSettings'
import { cn } from '@/shared/lib/cn'
import { IconHeadphones } from '@/shared/components/Icons'
import { ScrollArea } from '@/shared/ui/scroll-area'
import { EditorMaskPanel, type CoverApplyRange } from '@/features/editor/EditorMaskPanel'
import {
  type PixelBox,
  type PropTab,
  autoSubtitleFontSize,
  CAPTION_COLORS,
  CAPTION_FONT_PRESETS,
  COVER_MASK_STYLES,
  NumField,
  PropLabel,
  TabSvg,
  coverMaskPreviewStyle,
  formatSpeedX,
  formatTimecode,
  isOcrOverlayLayout,
  parseTimecode,
  segmentHasDub,
  speedStatusLines,
} from '@/features/editor/lib'

/** «Ưu tiên 0.8»: dub ở đồng hồ 0.8 rồi nâng 1× → giọng phát ×(bake/ttsBake).
 *  UI Tốc độ TTS làm việc theo TỐC ĐỘ PHÁT THỰC; lưu xuống manual = eff/ratio. */
function ttsPlayRatio(ttsBake: number | undefined, bakedSpeed: number | undefined): number {
  const tb = typeof ttsBake === 'number' && ttsBake > 0.2 && ttsBake <= 2.5 ? ttsBake : 1
  const bk = typeof bakedSpeed === 'number' && bakedSpeed > 0.2 ? Math.max(0.5, Math.min(2, bakedSpeed)) : 1
  return bk / tb
}

const TTS_PRESETS = [0.75, 0.9, 1, 1.2, 1.3, 1.5]
const clampTtsManual = (v: number) => Math.max(0.75, Math.min(1.5, Math.round(v * 1000) / 1000))

type Props = {
  effectivePropTab: PropTab
  setPropTab: (tab: PropTab) => void
  setTool: (tool: 'select' | 'cover' | 'text') => void
  busy: boolean
  segments: Segment[]
  settings: ProjectSettings
  onSettings: (settings: ProjectSettings) => void
  applyCaptionToAll: boolean
  setApplyCaptionToAll: (value: boolean) => void
  /** Seed and focus the directly draggable manual blur frame in the preview. */
  onEditManualBlurBand: () => void
  onPreviewCoverMaskOpacity: (opacity: number) => void
  onPreviewEffectOpacity: (overlay: TextOverlay, opacity: number) => void
  /** Retained for the hidden compatibility views; project actions render on the left rail. */
  onRunPipeline?: (previewSec: number, settingsOverride?: ProjectSettings) => void | Promise<void>
  onCancel?: () => void
  onOpenExport: () => void
  onUpdateSpeakerProfile: (id: string, patch: { name?: string; color?: string; voice?: string }) => void
  onOpenProjectSpeakers: () => void
  voices: { id: string; name: string }[]
  selected: Segment | undefined
  selectedOverlay: TextOverlay | null
  bboxSeg: Segment | null | undefined
  isOverlaySeg: boolean
  dubOn: boolean
  timelineDuration: number
  /** Playhead hiện tại (giây) — cho tab Vùng che chữ */
  playheadSec: number
  sourceWidth: number
  sourceHeight: number
  activeCaptionPx: number
  fontSizeDraft: number
  setFontSizeDraft: (v: number) => void
  fontSizeOptions: number[]
  applyAllLaneLabel: string
  showCoverBlur: boolean
  editSegment: (next: Segment, opts?: { textField?: string; skipHistory?: boolean }) => void | Promise<void>
  editOverlay: (
    overlay: TextOverlay,
    isNew?: boolean,
    opts?: { textField?: boolean; skipHistory?: boolean },
  ) => void | Promise<void>
  onOverlayDelete: (overlayId: string) => void
  setSelectedOverlayId: (id: string | null) => void
  onSegmentsReplace: (segments: Segment[], opts?: { persist?: boolean }) => void | Promise<void>
  pushHistory: () => void
  // TTS
  ttsBusy: boolean
  ttsError: string | null
  previewTts: (forSeg?: Segment) => Promise<void>
  playSegmentDub: (seg: Segment) => void
  // Caption apply helpers
  applyFontFamily: (scope: 'one' | 'all', family: string) => Promise<void>
  applyFontSize: (scope: 'one' | 'all', sizeOverride?: number) => void
  applyCaptionColor: (scope: 'one' | 'all', textColor: string) => void
  applyCaptionModeAll: (mode: 'cover' | 'below' | 'above' | 'none') => void
  // Tốc độ video (bake)
  speedStatus: ReturnType<typeof speedStatusLines>
  speedDraft: number
  setSpeedDraft: (v: number) => void
  speedBusy: boolean
  speedCancelling: boolean
  speedError: string | null
  setSpeedError: (e: string | null) => void
  appliedSpeedX: number
  hasBakedSpeed: boolean
  /** Bake hiện tại của file — hiện tốc độ PHÁT thực của TTS (ttsSpeed × bake/ttsBake) */
  bakedSpeed?: number
  applyVideoSpeed: (scope: 'one' | 'all', speed?: number) => void
  cancelVideoSpeed: () => Promise<void>
  // Âm thanh / stem
  wantNoVocals: boolean
  stemStatus: 'off' | 'loading' | 'ready' | 'error'
  stemProgress: number
  stemError: string | null
  setStemRetry: React.Dispatch<React.SetStateAction<number>>
  globalVoice: string
  setGlobalVoice: (v: string) => void
  globalTtsVolume: number
  setGlobalTtsVolume: (v: number) => void
  globalTtsSpeed: number
  setGlobalTtsSpeed: (v: number) => void
  onDub?: () => void
  jobStep: string
  jobProgress: number
  // Vùng che chữ
  coverMaskStyle: string
  coverMaskColor: string
  coverMaskOpacity: number
  selectedBox: PixelBox | null
  commitCoverBox: (patch: Partial<PixelBox>) => void
  stretchCoverFullWidth: () => void
  applyCoverMaskToAll: (range?: CoverApplyRange) => void
  resetOcrRegion: (scope: 'one' | 'all') => void
  // Logo / watermark
  logoDraft: TextOverlay | null
  setLogoDraft: (draft: TextOverlay | null) => void
  fitTextLogo: (logo: TextOverlay, text?: string, fontSize?: number) => TextOverlay
  logoError: string | null
  logoApplying: boolean
  logoToggleDisabled: boolean
  logoToggleRemoves: boolean
  unapplyLogo: () => void
  applyLogoDraft: () => Promise<void>
  appliedLogo: TextOverlay | null
  editLogo: (source?: 'text' | 'image' | 'icon') => TextOverlay
}

export function EditorPropertiesPanel({
  effectivePropTab,
  setPropTab,
  setTool,
  busy,
  segments,
  settings,
  onSettings,
  applyCaptionToAll,
  setApplyCaptionToAll,
  onEditManualBlurBand,
  onPreviewCoverMaskOpacity,
  onPreviewEffectOpacity,
  onRunPipeline,
  onCancel,
  onOpenExport,
  onUpdateSpeakerProfile,
  onOpenProjectSpeakers,
  voices,
  selected,
  selectedOverlay,
  bboxSeg,
  isOverlaySeg,
  dubOn,
  timelineDuration,
  playheadSec,
  sourceWidth,
  sourceHeight,
  activeCaptionPx,
  fontSizeDraft,
  setFontSizeDraft,
  fontSizeOptions,
  applyAllLaneLabel,
  showCoverBlur,
  editSegment,
  editOverlay,
  onOverlayDelete,
  setSelectedOverlayId,
  onSegmentsReplace,
  pushHistory,
  ttsBusy,
  ttsError,
  previewTts,
  playSegmentDub,
  applyFontFamily,
  applyFontSize,
  applyCaptionColor,
  applyCaptionModeAll,
  speedStatus,
  speedDraft,
  setSpeedDraft,
  speedBusy,
  speedCancelling,
  speedError,
  setSpeedError,
  appliedSpeedX,
  hasBakedSpeed,
  bakedSpeed,
  applyVideoSpeed,
  cancelVideoSpeed,
  wantNoVocals,
  stemStatus,
  stemProgress,
  stemError,
  setStemRetry,
  globalVoice,
  setGlobalVoice,
  globalTtsVolume,
  setGlobalTtsVolume,
  globalTtsSpeed,
  setGlobalTtsSpeed,
  onDub,
  jobStep,
  jobProgress,
  coverMaskStyle,
  coverMaskColor,
  coverMaskOpacity,
  selectedBox,
  commitCoverBox,
  stretchCoverFullWidth,
  applyCoverMaskToAll,
  resetOcrRegion,
  logoDraft,
  setLogoDraft,
  fitTextLogo,
  logoError,
  logoApplying,
  logoToggleDisabled,
  logoToggleRemoves,
  unapplyLogo,
  applyLogoDraft,
  appliedLogo,
  editLogo,
}: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const captionScope = applyCaptionToAll ? 'all' : 'one'
  const [coverMaskOpacityDraft, setCoverMaskOpacityDraft] = React.useState(settings.coverMaskOpacity ?? 0)
  const coverMaskOpacityDraftRef = React.useRef(coverMaskOpacityDraft)
  const coverMaskOpacityGestureRef = React.useRef(false)
  const [effectOpacityDraft, setEffectOpacityDraft] = React.useState<number | null>(null)
  const effectOpacityDraftRef = React.useRef<number | null>(null)
  const effectOpacityGestureRef = React.useRef(false)

  React.useEffect(() => {
    if (coverMaskOpacityGestureRef.current) return
    const next = settings.coverMaskOpacity ?? 0
    coverMaskOpacityDraftRef.current = next
    setCoverMaskOpacityDraft(next)
  }, [settings.coverMaskOpacity])

  React.useEffect(() => {
    if (effectOpacityGestureRef.current) return
    const next = selectedOverlay?.maskOpacity ?? 0
    effectOpacityDraftRef.current = next
    setEffectOpacityDraft(selectedOverlay?.kind === 'effect' ? next : null)
  }, [selectedOverlay?.id, selectedOverlay?.kind, selectedOverlay?.maskOpacity])

  const previewCoverMaskOpacity = (next: number) => {
    coverMaskOpacityGestureRef.current = true
    coverMaskOpacityDraftRef.current = next
    setCoverMaskOpacityDraft(next)
    onPreviewCoverMaskOpacity(next)
  }
  const commitCoverMaskOpacity = () => {
    if (!coverMaskOpacityGestureRef.current) return
    coverMaskOpacityGestureRef.current = false
    const next = coverMaskOpacityDraftRef.current
    if (next !== (settings.coverMaskOpacity ?? 0)) onSettings({ ...settings, coverMaskOpacity: next })
  }
  const previewEffectOpacity = (next: number) => {
    if (!selectedOverlay || selectedOverlay.kind !== 'effect') return
    effectOpacityGestureRef.current = true
    effectOpacityDraftRef.current = next
    setEffectOpacityDraft(next)
    onPreviewEffectOpacity(selectedOverlay, next)
  }
  const commitEffectOpacity = () => {
    if (!effectOpacityGestureRef.current || !selectedOverlay || selectedOverlay.kind !== 'effect') return
    effectOpacityGestureRef.current = false
    const next = effectOpacityDraftRef.current
    if (next !== null && next !== (selectedOverlay.maskOpacity ?? 0)) editOverlay({ ...selectedOverlay, maskOpacity: next })
  }
  const coverMaskLabel = (id: string, label: string) => id === 'feather' ? t('Mờ tan mép', 'Feathered blur') : label
  const speakerProfiles = React.useMemo(() => resolvedSpeakerProfiles(segments, settings, locale), [segments, settings, locale])
  const selectedSpeaker = selected?.speaker
    ? speakerProfiles.find((profile) => profile.id === selected.speaker)
    : undefined
  // OCR/cover watermarks are persisted as overlays for timing, but their box
  // is not a caption bbox. Keep this distinction here as well as on canvas.
  const selectedIsWatermark = Boolean(
    selectedOverlay && (
      selectedOverlay.watermarkSource
      || selectedOverlay.id === 'auto-watermark-ai-generated'
      || selectedOverlay.id === 'auto-watermark-static-logo'
    ),
  )
  // Kept only so existing state remains safe during editor-session restoration.
  const [previewRunSec, setPreviewRunSec] = React.useState(() => Math.max(5, Number(settings.previewSec) || 30))
  const PROP_TABS: { key: PropTab; label: string; icon: React.ReactNode; hidden?: boolean }[] = [
    {
      key: 'caption', label: t('Phụ đề', 'Captions'),
      icon: <TabSvg><polyline points="4 7 4 4 20 4 20 7" /><line x1="9" y1="20" x2="15" y2="20" /><line x1="12" y1="4" x2="12" y2="20" /></TabSvg>,
    },
    {
      key: 'video', label: 'Video',
      icon: <TabSvg><rect x="2" y="2" width="20" height="20" rx="2.18" /><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5" /></TabSvg>,
    },
    {
      key: 'audio', label: t('Âm thanh', 'Audio'),
      icon: <TabSvg><path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3" /></TabSvg>,
    },
    {
      key: 'mask', label: selectedIsWatermark ? t('Vùng che logo', 'Logo mask') : t('Vùng che chữ', 'Text mask'),
      icon: <TabSvg><rect x="4" y="4" width="16" height="16" rx="1" strokeDasharray="3 3" /></TabSvg>,
    },
    {
      key: 'overlay', label: logoDraft || (selectedOverlay?.kind === 'logo' && !selectedIsWatermark)
        ? 'Logo'
        : selectedOverlay?.track === 'ocr'
          ? 'Caption 2 (OCR)'
          : t('Lớp chữ', 'Text overlay'), hidden: !selectedOverlay && !logoDraft,
      icon: <TabSvg><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></TabSvg>,
    },
  ]

  return (
                      <div className="panel bg-background flex h-full overflow-hidden rounded-sm border border-border">

                    {/* Vertical tab rail — luôn hiện đủ tab (Âm thanh + Vùng che chữ) */}
                    <div className="flex shrink-0 flex-col gap-0.5 border-r border-border p-1 scrollbar-hidden overflow-y-auto">
                      {PROP_TABS.filter((t) => !t.hidden).map((tab) => (
                        <button
                          key={tab.key}
                          type="button"
                          aria-label={tab.label}
                          title={tab.label}
                          className={cn(
                            'flex h-8 w-8 shrink-0 items-center justify-center rounded-md transition-colors',
                            effectivePropTab === tab.key
                              ? 'bg-accent text-accent-foreground'
                              : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
                          )}
                          onClick={() => {
                            if (tab.key === 'overlay' && appliedLogo && !logoDraft && !selectedIsWatermark) editLogo(appliedLogo.logoSource ?? 'text')
                            else setPropTab(tab.key)
                          }}
                          onPointerDown={() => {
                            if (tab.key === 'mask') setTool('cover')
                          }}
                        >
                          {tab.icon}
                        </button>
                      ))}
                    </div>

                    {/* Tab content */}
                    <ScrollArea className="flex-1 scrollbar-hidden">
                      <div className="p-3 flex flex-col gap-3">
                        <div className="text-sm text-muted-foreground pb-1 border-b border-border">
                          {selected
                            ? `${PROP_TABS.find((t) => t.key === effectivePropTab)?.label} — Đoạn #${String(selected.index).padStart(2, '0')}`
                            : `${PROP_TABS.find((t) => t.key === effectivePropTab)?.label ?? 'Thuộc tính'} — ${t('Tất cả', 'All')}`}
                        </div>

                        {(effectivePropTab as string) === 'workflow' && (
                          <section className="space-y-3">
                            <p className="text-[11px] leading-snug text-muted-foreground">{t('Chạy nhận dạng, dịch, lồng tiếng và xuất video ngay trong editor.', 'Run recognition, translation, dubbing, and export directly in the editor.')}</p>
                            <div className="grid grid-cols-2 gap-2">
                              <PropLabel label={t('Nhận dạng', 'Recognition')}>
                                <select className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs" value={settings.engine} disabled={busy} onChange={(event) => { const engine = event.target.value as ProjectSettings['engine']; onSettings({ ...settings, engine, translator: normalizeTranslatorForEngine(engine, settings.translator) }) }}>
                                  <option value="whisper">Whisper</option><option value="capcut">{t('CapCut cloud', 'CapCut cloud')}</option><option value="paddleocr">OCR</option><option value="subtitle">SRT</option>
                                </select>
                              </PropLabel>
                              <PropLabel label={t('Công cụ dịch', 'Translator')}>
                                <select className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs" value={settings.translator} disabled={busy} onChange={(event) => onSettings({ ...settings, translator: event.target.value as ProjectSettings['translator'] })}>
                                  {translatorOptions(settings.engine).map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
                                </select>
                              </PropLabel>
                              <PropLabel label={t('Ngôn ngữ gốc', 'Source language')}>
                                <select className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs" value={settings.sourceLang} disabled={busy} onChange={(event) => onSettings({ ...settings, sourceLang: event.target.value })}>
                                  <option value="auto">{t('Tự động', 'Auto')}</option><option value="zh">中文</option><option value="en">English</option><option value="ja">日本語</option><option value="ko">한국어</option><option value="vi">Tiếng Việt</option>
                                </select>
                              </PropLabel>
                              <PropLabel label={t('Dịch sang', 'Translate to')}>
                                <select className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs" value={settings.targetLang} disabled={busy} onChange={(event) => onSettings({ ...settings, targetLang: event.target.value })}>
                                  <option value="vi">Tiếng Việt</option><option value="en">English</option><option value="zh">中文</option><option value="ja">日本語</option><option value="ko">한국어</option><option value="none">{t('Không dịch', 'No translation')}</option>
                                </select>
                              </PropLabel>
                            </div>
                            <div className="rounded-md border border-border p-2">
                              <div className="flex items-center justify-between gap-2"><b className="text-xs text-foreground">{t('Phạm vi chạy', 'Run range')}</b><input className="h-7 w-16 rounded border border-border bg-background px-1.5 text-right text-xs" type="number" min={5} max={3600} value={previewRunSec} disabled={busy} aria-label={t('Số giây preview', 'Preview seconds')} onChange={(event) => setPreviewRunSec(Math.max(5, Math.min(3600, Number(event.target.value) || 5)))} /></div>
                              <div className="mt-2 grid grid-cols-2 gap-1.5">
                                <button type="button" disabled={busy || !onRunPipeline} className="rounded-md border border-border px-2 py-2 text-xs font-medium hover:bg-accent disabled:opacity-50" onClick={() => { const next = { ...settings, previewSec: previewRunSec }; onSettings(next); void onRunPipeline?.(previewRunSec, next) }}>{t('Chạy preview', 'Run preview')}</button>
                                <button type="button" disabled={busy || !onRunPipeline} className="rounded-md bg-primary px-2 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50" onClick={() => void onRunPipeline?.(0, settings)}>{t('Chạy toàn video', 'Run full video')}</button>
                              </div>
                              {busy && <div className="mt-2 flex items-center justify-between gap-2 text-[11px] text-muted-foreground"><span className="min-w-0 truncate">{jobStep || t('Đang xử lý', 'Processing')} · {Math.round(jobProgress || 0)}%</span>{onCancel && <button type="button" className="shrink-0 text-xs font-medium text-destructive hover:underline" onClick={onCancel}>{t('Hủy', 'Cancel')}</button>}</div>}
                            </div>
                            <div className="grid grid-cols-2 gap-1.5">
                              <button type="button" disabled={busy || segments.length === 0 || !onDub} className="rounded-md border border-primary/40 bg-primary/10 px-2 py-2 text-xs font-medium text-primary hover:bg-primary/20 disabled:opacity-50" onClick={() => void onDub?.()}>{settings.speakerDiarization ? t('Tạo TTS theo vai', 'Generate TTS by role') : t('Tạo TTS', 'Generate TTS')}</button>
                              <button type="button" disabled={busy} className="rounded-md border border-border px-2 py-2 text-xs font-medium hover:bg-accent disabled:opacity-50" onClick={onOpenExport}>{t('Xuất video', 'Export video')}</button>
                            </div>
                          </section>
                        )}

                        {(effectivePropTab as string) === 'speakers' && (
                          <section className="space-y-2">
                            {speakerProfiles.length === 0 ? <p className="py-3 text-center text-[11px] leading-snug text-muted-foreground">{t('Chưa có người nói. Bật Tách người nói ở tab Âm thanh rồi chạy Nhận dạng & dịch.', 'No speakers yet. Enable speaker separation in Audio, then run recognition and translation.')}</p> : <>
                              <p className="text-[11px] text-muted-foreground">{locale === 'en' ? `${speakerProfiles.length} roles detected` : `Đã nhận ${speakerProfiles.length} vai`}</p>
                              {speakerProfiles.map((profile) => {
                                const owned = segments.filter((segment) => segment.speaker === profile.id)
                                const seconds = owned.reduce((sum, segment) => sum + Math.max(0, segment.end - segment.start), 0)
                                return <div key={profile.id} className="space-y-1.5 rounded-md border border-border bg-background p-2" style={{ borderLeft: `4px solid ${profile.color}` }}>
                                  <div className="flex gap-1.5"><input type="color" className="size-8 shrink-0 rounded border border-border bg-transparent p-0.5" value={profile.color} disabled={busy} aria-label={`${t('Màu', 'Color')} ${profile.name}`} onChange={(event) => onUpdateSpeakerProfile(profile.id, { color: event.target.value })} /><input className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 text-xs font-medium" value={profile.name} list={`speaker-role-${profile.id}`} disabled={busy} aria-label={`${t('Tên', 'Name')} ${profile.id}`} onChange={(event) => onUpdateSpeakerProfile(profile.id, { name: event.target.value })} /><datalist id={`speaker-role-${profile.id}`}>{speakerRoleOptions(locale).map((role) => <option key={role} value={role} />)}</datalist></div>
                                  <select className="h-8 w-full rounded-md border border-border bg-background px-2 text-[11px]" value={profile.voice || ''} disabled={busy} onChange={(event) => onUpdateSpeakerProfile(profile.id, { voice: event.target.value })}><option value="">{t('Giọng mặc định', 'Default voice')}</option>{voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}</select>
                                  <div className="flex justify-between text-[10px] text-muted-foreground"><span>{owned.length} {t('đoạn', 'segments')}</span><span>{formatTimecode(seconds)}</span></div>
                                </div>
                              })}
                            </>}
                          </section>
                        )}

                        {/* Blur band — project-level, hiện dù có/không có segment đang chọn */}
                        {effectivePropTab === 'caption' && (
                          <div className="border border-border rounded-md p-2 space-y-1.5 bg-muted/30">
                            <label className="flex items-center gap-1.5 cursor-pointer select-none">
                              <input
                                type="checkbox"
                                className="size-3.5 accent-primary"
                                checked={(settings.blurBandMode ?? 'auto') !== 'off'}
                                onChange={(e) =>
                                  onSettings({ ...settings, blurBandMode: e.target.checked ? 'auto' : 'off' })
                                }
                              />
                              <span className="text-xs font-medium">{t('Vùng làm mờ cố định', 'Persistent blur zone')}</span>
                            </label>
                            {(settings.blurBandMode ?? 'auto') !== 'off' && (
                              <>
                                <select
                                  className="w-full rounded-md border border-border bg-input px-2 py-1 text-xs outline-none focus:border-ring"
                                  value={settings.blurBandMode ?? 'auto'}
                                  onChange={(e) => {
                                    if (e.target.value === 'manual') onEditManualBlurBand()
                                    else onSettings({ ...settings, blurBandMode: 'auto' })
                                  }}
                                >
                                  <option value="auto">{t('Tự động (OCR phát hiện vị trí)', 'Auto (OCR-detected position)')}</option>
                                  <option value="manual">{t('Thủ công (kéo khung trên preview)', 'Manual (drag frame on preview)')}</option>
                                </select>
                                {settings.blurBandMode === 'manual' && (
                                  <div className="space-y-1">
                                    <button
                                      type="button"
                                      className="w-full rounded-md border border-dashed border-cyan-500 bg-cyan-500/10 px-2 py-1.5 text-[11px] text-cyan-400 hover:bg-cyan-500/20 transition-colors"
                                      onClick={onEditManualBlurBand}
                                    >
                                      {t('Kéo khung làm mờ trên preview →', 'Drag blur zone frame on preview →')}
                                    </button>
                                    {settings.blurBandRegion && (
                                      <p className="text-[10px] text-muted-foreground">
                                        {t('Vùng: ', 'Zone: ')}
                                        {Math.round(settings.blurBandRegion.x * 100)}%,{' '}
                                        {Math.round(settings.blurBandRegion.y * 100)}%{' '}
                                        {Math.round(settings.blurBandRegion.w * 100)}×{Math.round(settings.blurBandRegion.h * 100)}%
                                      </p>
                                    )}
                                    {!settings.blurBandRegion && (
                                      <p className="text-[10px] text-amber-400">
                                        {t('Chưa định vị — kéo khung để chọn vùng.', 'Not positioned — drag the frame to select a zone.')}
                                      </p>
                                    )}
                                  </div>
                                )}
                              </>
                            )}
                          </div>
                        )}

                        {effectivePropTab === 'caption' && selected && (
                          <>
                            <label className="flex items-center gap-2 rounded-md border border-border bg-muted/30 px-2 py-1.5 text-xs font-medium cursor-pointer select-none">
                              <input
                                type="checkbox"
                                className="size-3.5 accent-primary"
                                checked={applyCaptionToAll}
                                disabled={busy}
                                onChange={(e) => setApplyCaptionToAll(e.target.checked)}
                              />
                              {t('Áp dụng cho tất cả phụ đề', 'Apply to all captions')}
                            </label>
                            <PropLabel label="Ngôn ngữ gốc">
                              <textarea
                                className="w-full rounded-md border border-border bg-input px-2 py-1.5 text-xs resize-none outline-none focus:border-ring"
                                value={selected.source}
                                rows={2}
                                disabled={busy}
                                onChange={(e) => editSegment({ ...selected, source: e.target.value }, { textField: 'source' })}
                              />
                            </PropLabel>
                            <PropLabel label="Bản dịch">
                              <textarea
                                className="w-full rounded-md border border-border bg-input px-2 py-1.5 text-xs resize-none outline-none focus:border-ring"
                                value={selected.translation}
                                rows={4}
                                disabled={busy}
                                onChange={(e) => editSegment({ ...selected, translation: e.target.value, captionLayout: null }, { textField: 'translation' })}
                              />
                            </PropLabel>

                            {isOverlaySeg && (
                              <label className="flex items-center gap-2 text-xs cursor-pointer py-0.5">
                                <input
                                  type="checkbox"
                                  checked={dubOn}
                                  disabled={busy}
                                  onChange={(e) => editSegment({
                                    ...selected,
                                    dub: e.target.checked,
                                    ...(e.target.checked ? {} : { audioUrl: undefined, audioFile: undefined, audioDuration: undefined }),
                                  })}
                                  className="accent-primary"
                                />
                                Lồng tiếng
                              </label>
                            )}

                            <PropLabel label="Giọng đọc">
                              <div className="flex items-center gap-1.5">
                                <select
                                  className="min-w-0 flex-1 rounded-md border border-border bg-input px-2 py-1 text-xs outline-none focus:border-ring"
                                  value={selected.voice || settings.defaultVoice}
                                  disabled={busy || (isOverlaySeg && !dubOn)}
                                  onChange={(e) => editSegment({ ...selected, voice: e.target.value, ...(isOverlaySeg ? { dub: true } : {}) })}
                                >
                                  {voices.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
                                </select>
                                <select
                                  className="w-[72px] shrink-0 rounded-md border border-border bg-input px-1.5 py-1 text-xs outline-none focus:border-ring"
                                  value={String(selected.ttsSpeed ?? 1.1)}
                                  disabled={busy || (isOverlaySeg && !dubOn)}
                                  title="Tốc độ TTS"
                                  aria-label="Tốc độ TTS"
                                  onChange={(e) => editSegment({
                                    ...selected,
                                    ttsSpeed: Number(e.target.value),
                                    ...(isOverlaySeg ? { dub: true } : {}),
                                  }, { textField: 'ttsSpeed' })}
                                >
                                  {Array.from(
                                    { length: 16 },
                                    (_, i) => Number((0.75 + i * 0.05).toFixed(2)),
                                  ).map((speed) => (
                                    <option key={speed} value={speed}>{speed.toFixed(2)}×</option>
                                  ))}
                                </select>
                                <button
                                  type="button"
                                  className="shrink-0 rounded-md border border-border bg-accent hover:bg-muted px-2.5 py-1 text-xs transition-colors disabled:opacity-50 flex items-center gap-1"
                                  disabled={busy || ttsBusy || !selected.translation.trim() || (isOverlaySeg && !dubOn)}
                                  title="Nghe và áp dụng TTS"
                                  onClick={() => void previewTts({
                                    ...selected,
                                    ttsSpeed: selected.ttsSpeed ?? 1.1,
                                  })}
                                >
                                  {ttsBusy ? '…' : <><IconHeadphones size={13} /> Nghe và áp dụng</>}
                                </button>
                              </div>
                            </PropLabel>
                            {ttsError && <p className="text-xs text-destructive">{ttsError}</p>}

                            <div className="border-t border-border pt-3 flex flex-col gap-2">
                              <div className="grid grid-cols-[1fr_auto] gap-1.5">
                                <PropLabel label="Phông chữ">
                                  <select
                                    className="w-full rounded-md border border-border bg-input px-2 py-1 text-xs outline-none focus:border-ring"
                                    value={selected.fontFamily || settings.subtitleFontFamily || 'system'}
                                    disabled={busy}
                                    onChange={(e) => void applyFontFamily(captionScope, e.target.value)}
                                  >
                                    {CAPTION_FONT_PRESETS.map((font) => (
                                      <option key={font.id} value={font.id} style={{ fontFamily: font.css }}>{font.label}</option>
                                    ))}
                                  </select>
                                </PropLabel>
                                <PropLabel label="Màu chữ">
                                  <input
                                    type="color"
                                    className="h-7 w-10 cursor-pointer rounded border border-border bg-transparent"
                                    value={selected.textColor || settings.captionTextColor || '#ffffff'}
                                    disabled={busy}
                                    onChange={(e) => applyCaptionColor(captionScope, e.target.value)}
                                  />
                                </PropLabel>
                              </div>
                              <div className="flex flex-wrap items-center gap-1.5">
                                {CAPTION_COLORS.map((color) => (
                                  <button
                                    key={color}
                                    type="button"
                                    title={color}
                                    className={cn(
                                      'size-5 rounded-full border shrink-0',
                                      (selected.textColor || settings.captionTextColor || '#ffffff').toLowerCase() === color
                                        ? 'border-primary ring-1 ring-primary'
                                        : color === '#000000' || color === '#1e293b'
                                          ? 'border-border/80'
                                          : 'border-border',
                                    )}
                                    style={{ background: color }}
                                    disabled={busy}
                                    onClick={() => applyCaptionColor(captionScope, color)}
                                  />
                                ))}
                              </div>
                              <PropLabel label={`Cỡ chữ (xem trước ~${activeCaptionPx}px)`}>
                                <select
                                  className="w-full rounded-md border border-border bg-input px-2 py-1 text-xs outline-none focus:border-ring"
                                  value={String(fontSizeDraft)}
                                  disabled={busy}
                                  onChange={(e) => applyFontSize(captionScope, Number(e.target.value))}
                                >
                                  <option value="0">
                                    {isOverlaySeg
                                      ? t('Tự động theo khung (đủ đọc)', 'Auto fit to box (readable)')
                                      : t(
                                          `Tự động (${autoSubtitleFontSize(sourceWidth, sourceHeight)}px${settings.subtitleFontSize > 0 ? ` · dự án ${settings.subtitleFontSize}px` : ''})`,
                                          `Auto (${autoSubtitleFontSize(sourceWidth, sourceHeight)}px${settings.subtitleFontSize > 0 ? ` · project ${settings.subtitleFontSize}px` : ''})`,
                                        )}
                                  </option>
                                  {fontSizeOptions.map((px) => (
                                    <option key={px} value={px}>{px} px</option>
                                  ))}
                                </select>
                              </PropLabel>
                              <div className="grid grid-cols-2 gap-1.5">
                                <button
                                  type="button"
                                  className="rounded-md border border-border bg-accent hover:bg-muted px-2 py-1.5 text-[11px] transition-colors disabled:opacity-50"
                                  disabled={busy || !selected}
                                  onClick={() => applyFontSize(captionScope)}
                                >
                                  {applyCaptionToAll ? t('Áp dụng tất cả', 'Apply all') : t('Áp dụng đoạn này', 'Apply this caption')}
                                </button>
                                <button
                                  type="button"
                                  className="rounded-md border border-primary/40 bg-primary/10 text-primary hover:bg-primary/20 px-2 py-1.5 text-[11px] transition-colors disabled:opacity-50"
                                  disabled={busy || !(selected || bboxSeg)}
                                  title={`Chỉ lane «${applyAllLaneLabel}»`}
                                  onClick={() => applyFontSize('all')}
                                >
                                  Áp {applyAllLaneLabel}
                                </button>
                              </div>
                              {(selected?.fontSize ?? 0) > 0 && (
                                <button
                                  type="button"
                                  className="text-[11px] text-muted-foreground hover:text-foreground underline-offset-2 hover:underline"
                                  onClick={() => applyFontSize(captionScope, 0)}
                                >
                                  {applyCaptionToAll ? t('Reset tất cả về tự động', 'Reset all to automatic') : t('Reset đoạn này về tự động', 'Reset this caption to automatic')}
                                </button>
                              )}
                              {isOverlaySeg && (
                                <p className="text-[11px] text-muted-foreground leading-snug">
                                  Đổi cỡ → áp dụng ngay. Khung dọc/mid/nhãn nới theo chữ.
                                </p>
                              )}

                              <PropLabel label="Chèn phụ đề">
                                <select
                                  className="w-full rounded-md border border-border bg-input px-2 py-1 text-xs outline-none focus:border-ring"
                                  value={
                                    settings.coverHardsubs && settings.burnSubs ? 'cover'
                                      : !settings.burnSubs ? 'none'
                                      : settings.captionPlacement === 'above' ? 'above' : 'below'
                                  }
                                  disabled={busy}
                                  onChange={(e) => {
                                    const mode = e.target.value as 'cover' | 'below' | 'above' | 'none'
                                    applyCaptionModeAll(mode)
                                  }}
                                >
                                  <option value="cover">{settings.targetLang === 'none' ? 'Che chữ cũ + chèn chữ gốc' : 'Che chữ cũ + chèn dịch'}</option>
                                  <option value="below">{settings.targetLang === 'none' ? 'Chèn chữ gốc phía dưới' : 'Chèn dịch phía dưới'}</option>
                                  <option value="above">{settings.targetLang === 'none' ? 'Chèn chữ gốc phía trên' : 'Chèn dịch phía trên'}</option>
                                  <option value="none">Không chèn chữ</option>
                                </select>
                              </PropLabel>
                              {showCoverBlur && (
                                <p className="text-[10px] text-muted-foreground leading-snug">
                                  Kéo khung <strong className="text-violet-400">tím</strong> trên preview phủ đúng chữ gốc.
                                  Chữ dịch căn giữa khung tím. Chi tiết ở tab <strong>Vùng che chữ</strong>.
                                </p>
                              )}
                            </div>
                          </>
                        )}

                        {/* Phụ đề — Tất cả (CapCut-style) */}
                        {effectivePropTab === 'caption' && !selected && (
                          <>
                            <p className="text-[11px] text-muted-foreground leading-relaxed">
                              Style phụ đề toàn dự án — phông, màu, nền, bbox che, hiệu ứng. Áp dụng ngay khi đổi.
                            </p>

                            <PropLabel label="Chèn phụ đề">
                              <select
                                className="w-full rounded-md border border-border bg-input px-2 py-1 text-xs outline-none focus:border-ring"
                                value={
                                  settings.coverHardsubs && settings.burnSubs ? 'cover'
                                    : !settings.burnSubs ? 'none'
                                    : settings.captionPlacement === 'above' ? 'above' : 'below'
                                }
                                disabled={busy}
                                onChange={(e) => {
                                  applyCaptionModeAll(e.target.value as 'cover' | 'below' | 'above' | 'none')
                                }}
                              >
                                <option value="cover">{settings.targetLang === 'none' ? 'Che chữ cũ + chèn chữ gốc' : 'Che chữ cũ + chèn dịch'}</option>
                                <option value="below">{settings.targetLang === 'none' ? 'Chèn chữ gốc phía dưới' : 'Chèn dịch phía dưới'}</option>
                                <option value="above">{settings.targetLang === 'none' ? 'Chèn chữ gốc phía trên' : 'Chèn dịch phía trên'}</option>
                                <option value="none">Không chèn chữ</option>
                              </select>
                            </PropLabel>

                            <div className="border-t border-border pt-2 space-y-2">
                              <p className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">Chữ</p>
                              <div className="grid grid-cols-2 gap-1.5">
                                <PropLabel label="Phông chữ">
                                  <select
                                    className="w-full rounded-md border border-border bg-input px-2 py-1 text-xs outline-none focus:border-ring"
                                    value={settings.subtitleFontFamily || 'system'}
                                    disabled={busy}
                                    onChange={(e) => void applyFontFamily('all', e.target.value)}
                                  >
                                    {CAPTION_FONT_PRESETS.map((f) => (
                                      <option key={f.id} value={f.id} style={{ fontFamily: f.css }}>{f.label}</option>
                                    ))}
                                  </select>
                                </PropLabel>
                                <PropLabel label="Cỡ chữ">
                                  <select
                                    className="w-full rounded-md border border-border bg-input px-2 py-1 text-xs outline-none focus:border-ring"
                                    value={String(fontSizeDraft)}
                                    disabled={busy}
                                    onChange={(e) => {
                                      const v = Number(e.target.value)
                                      setFontSizeDraft(v)
                                      if (!busy && segments.length > 0) applyFontSize('all', v)
                                    }}
                                  >
                                    <option value="0">Tự động</option>
                                    {fontSizeOptions.map((px) => (
                                      <option key={px} value={px}>{px} px</option>
                                    ))}
                                  </select>
                                </PropLabel>
                              </div>
                              <PropLabel label="Màu chữ">
                                <div className="flex flex-wrap items-center gap-1.5">
                                  <input
                                    type="color"
                                    className="h-7 w-8 cursor-pointer rounded border border-border bg-transparent"
                                    value={settings.captionTextColor || '#ffffff'}
                                    disabled={busy}
                                    onChange={(e) => applyCaptionColor('all', e.target.value)}
                                  />
                                  {CAPTION_COLORS.map((color) => (
                                    <button
                                      key={color}
                                      type="button"
                                      title={color}
                                      className={cn(
                                        'size-5 rounded-full border shrink-0',
                                        (settings.captionTextColor || '#ffffff').toLowerCase() === color
                                          ? 'border-primary ring-1 ring-primary'
                                          : color === '#000000' || color === '#1e293b'
                                            ? 'border-border/80'
                                            : 'border-border',
                                      )}
                                      style={{ background: color }}
                                      disabled={busy}
                                      onClick={() => applyCaptionColor('all', color)}
                                    />
                                  ))}
                                </div>
                              </PropLabel>
                            </div>

                            <div className="border-t border-border pt-2 space-y-2">
                              <p className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">Bbox · che chữ gốc</p>
                              <PropLabel label="Kiểu mặt nạ (cover)">
                                <div className="grid grid-cols-2 gap-1">
                                  {COVER_MASK_STYLES.map(({ id, label }) => (
                                    <button
                                      key={id}
                                      type="button"
                                      className={cn(
                                        'flex-1 rounded-sm border px-1 py-1.5 text-[10px] transition-colors',
                                        (settings.coverMaskStyle ?? 'blur') === id
                                          ? 'border-primary text-primary bg-primary/10'
                                          : 'border-border text-muted-foreground hover:bg-accent',
                                      )}
                                      disabled={busy}
                                      onClick={() => onSettings({ ...settings, coverMaskStyle: id })}
                                    >
                                      {coverMaskLabel(id, label)}
                                    </button>
                                  ))}
                                </div>
                              </PropLabel>
                              <div className="flex items-center gap-2">
                                {(settings.coverMaskStyle ?? 'blur') !== 'mosaic' && (
                                  <input
                                    type="color"
                                    title="Màu mask"
                                    className="h-8 w-10 shrink-0 cursor-pointer rounded border border-border bg-transparent"
                                    value={settings.coverMaskColor || '#4c1d95'}
                                    disabled={busy}
                                    onChange={(e) => onSettings({ ...settings, coverMaskColor: e.target.value })}
                                  />
                                )}
                                <div className="min-w-0 flex-1 flex items-center gap-2">
                                  <input
                                    type="range"
                                    min={0}
                                    max={100}
                                    className="min-w-0 flex-1 accent-violet-500"
                                    value={coverMaskOpacityDraft}
                                    disabled={busy}
                                    onInput={(e) => previewCoverMaskOpacity(Number(e.currentTarget.value))}
                                    onPointerUp={commitCoverMaskOpacity}
                                    onPointerCancel={commitCoverMaskOpacity}
                                    onBlur={commitCoverMaskOpacity}
                                    onKeyUp={commitCoverMaskOpacity}
                                  />
                                  <span className="shrink-0 tabular-nums text-[10px] text-muted-foreground w-8 text-right">
                                    {coverMaskOpacityDraft}%
                                  </span>
                                </div>
                                <div
                                  className="h-8 w-10 shrink-0 rounded border border-border overflow-hidden"
                                  style={coverMaskPreviewStyle(
                                    settings.coverMaskStyle ?? 'blur',
                                    settings.coverMaskColor || '#4c1d95',
                                    coverMaskOpacityDraft,
                                  )}
                                  title="Xem trước mask"
                                />
                              </div>
                              <p className="text-[10px] text-muted-foreground leading-snug">
                                Che chữ: bật mode «Che chữ cũ». Kéo bbox trên preview / tab Vùng che chữ.
                              </p>
                              <button
                                type="button"
                                className="w-full rounded-md border border-border bg-accent hover:bg-muted px-2 py-1.5 text-[11px] disabled:opacity-50"
                                disabled={busy}
                                onClick={() => {
                                  setPropTab('mask')
                                  setTool('cover')
                                }}
                              >
                                Mở tab Vùng che chữ (bbox)
                              </button>
                            </div>
                          </>
                        )}

                         {effectivePropTab === 'video' && (() => {
                          const idx = selected ? segments.findIndex((s) => s.id === selected.id) : -1
                          const prevEnd = idx > 0 ? segments[idx - 1].end : 0
                          const nextStart = idx >= 0 ? (segments[idx + 1]?.start ?? timelineDuration) : timelineDuration
                          const minDur = 0.15
                          const draftX = formatSpeedX(speedDraft)
                          const fileX = formatSpeedX(appliedSpeedX)
                          const draftMatchesFile = Math.abs(speedDraft - appliedSpeedX) < 0.005
                          // Mặc định timeline luôn 1.00×. Chỉ nút Áp dụng tốc độ
                          // của người dùng mới thay đổi clock của file.
                          const defaultSpeedX = 1
                          const atDefault =
                            Math.abs(appliedSpeedX - defaultSpeedX) < 0.005
                            && Math.abs(speedDraft - defaultSpeedX) < 0.005
                            return (
                              <>
                                <div className="rounded-md border border-border bg-muted/20 p-2 space-y-2">
                                  <div className="flex items-center justify-between gap-3">
                                    <span className="text-xs font-medium">Biến đổi</span>
                                  </div>
                                  <PropLabel label={t(`Thu phóng ngang: ${Math.round(settings.videoScaleX ?? settings.videoScale ?? 100)}%`, `Horizontal zoom: ${Math.round(settings.videoScaleX ?? settings.videoScale ?? 100)}%`)}>
                                    <input
                                      type="range"
                                      min={1}
                                      max={500}
                                      step={1}
                                      value={settings.videoScaleX ?? settings.videoScale ?? 100}
                                      disabled={busy}
                                      className="w-full accent-primary"
                                      onChange={(e) => onSettings({
                                        ...settings,
                                        videoScaleX: Number(e.target.value),
                                      })}
                                    />
                                  </PropLabel>
                                  <PropLabel label={t(`Thu phóng dọc: ${Math.round(settings.videoScaleY ?? settings.videoScale ?? 100)}%`, `Vertical zoom: ${Math.round(settings.videoScaleY ?? settings.videoScale ?? 100)}%`)}>
                                    <input
                                      type="range"
                                      min={1}
                                      max={500}
                                      step={1}
                                      value={settings.videoScaleY ?? settings.videoScale ?? 100}
                                      disabled={busy}
                                      className="w-full accent-primary"
                                      onChange={(e) => onSettings({
                                        ...settings,
                                        videoScaleY: Number(e.target.value),
                                      })}
                                    />
                                  </PropLabel>
                                </div>
                                <div className="rounded-md border border-border bg-muted/30 px-2 py-1.5 space-y-0.5 text-[10px] leading-snug text-muted-foreground">
                                <p className="text-foreground/90">{speedStatus.inputLine}</p>
                                <p>{speedStatus.appliedLine}</p>
                                <p>{speedStatus.exportLine}</p>
                              </div>
                              <PropLabel label={t('Chọn tốc độ', 'Selected speed') + ': ' + draftX + (draftMatchesFile ? ' (= file ' + fileX + ')' : t(' · file đang ', ' · current file ') + fileX)}>
                                <input
                                  type="range"
                                  min={0.5}
                                  max={2}
                                  step={0.01}
                                  className="w-full accent-primary"
                                  value={speedDraft}
                                  disabled={busy && !speedBusy}
                                  onChange={(e) => {
                                    setSpeedError(null)
                                    const n = Math.round(Number(e.target.value) * 100) / 100
                                    // Chỉ đổi draft — bake khi bấm «Áp dụng»
                                    setSpeedDraft(n)
                                  }}
                                />
                              </PropLabel>
                              <div className="flex gap-1">
                                {[0.5, 0.75, 0.8, 1, 1.15, 1.5].map((v) => (
                                  <button
                                    key={v}
                                    type="button"
                                    className={cn(
                                      'flex-1 rounded-sm border px-1 py-1 text-[10px] transition-colors',
                                      Math.abs(speedDraft - v) < 0.005
                                        ? 'border-primary text-primary bg-primary/10'
                                        : 'border-border text-muted-foreground hover:text-foreground hover:bg-accent',
                                      hasBakedSpeed && Math.abs(appliedSpeedX - v) < 0.005
                                        && 'ring-1 ring-primary/40',
                                    )}
                                    disabled={busy && !speedBusy}
                                    onClick={() => {
                                      setSpeedError(null)
                                      // Chỉ đổi draft — bake khi bấm «Áp dụng»
                                      setSpeedDraft(v)
                                    }}
                                    title={
                                      hasBakedSpeed && Math.abs(appliedSpeedX - v) < 0.005
                                        ? t(`Đang ${formatSpeedX(v)} — chọn số khác rồi bấm Áp dụng`, `Đang ${formatSpeedX(v)} — chọn số khác rồi bấm Apply`)
                                        : t(`Chọn ${formatSpeedX(v)} rồi bấm Áp dụng`, `Chọn ${formatSpeedX(v)} rồi bấm Apply`)
                                    }
                                  >
                                    {formatSpeedX(v)}
                                  </button>
                                ))}
                              </div>
                              <button
                                type="button"
                                className="w-full rounded-md border border-primary/40 bg-primary/10 text-primary hover:bg-primary/20 px-2 py-1.5 text-[11px] transition-colors disabled:opacity-50"
                                disabled={(busy && !speedBusy) || speedCancelling}
                                title={
                                  speedBusy
                                    ? 'Hủy bake đang chạy (hoặc chọn tốc độ khác để thay thế)'
                                    : `Bake @ ${draftX}`
                                }
                                onClick={() => {
                                  if (speedBusy && Math.abs(speedDraft - appliedSpeedX) < 0.005) {
                                    void cancelVideoSpeed()
                                    return
                                  }
                                  // Áp dụng ngay — hủy txn cũ nếu đang chạy
                                  applyVideoSpeed('all', speedDraft)
                                }}
                              >
                                {speedCancelling
                                  ? 'Đang hủy…'
                                  : speedBusy
                                    ? `Đang bake… (Hủy / chọn số khác)`
                                    : draftMatchesFile && hasBakedSpeed
                                      ? `Đã khóa ${fileX} — chọn số khác để đổi`
                                      : t(`Áp dụng ${draftX} cho tất cả → file ${draftX}`, `Apply ${draftX} to all → file ${draftX}`)}
                              </button>
                              <button
                                type="button"
                                className="w-full rounded-md border border-border text-muted-foreground hover:text-foreground hover:bg-accent px-2 py-1.5 text-[11px] transition-colors disabled:opacity-50"
                                disabled={(busy && !speedBusy) || speedCancelling || speedBusy || atDefault}
                                title={t(`Đặt lại tốc độ mặc định ${formatSpeedX(defaultSpeedX)} (theo Khớp thời lượng) và áp dụng cho tất cả`, `Reset default speed ${formatSpeedX(defaultSpeedX)} (duration matching) and apply to all`)}
                                onClick={() => {
                                  setSpeedError(null)
                                  applyVideoSpeed('all', defaultSpeedX)
                                }}
                              >
                                {atDefault
                                  ? t(`Đang ở mặc định ${formatSpeedX(defaultSpeedX)}`, `At default speed ${formatSpeedX(defaultSpeedX)}`)
                                  : t(`Về mặc định ${formatSpeedX(defaultSpeedX)}`, `Reset to default ${formatSpeedX(defaultSpeedX)}`)}
                              </button>
                              {speedError && (
                                <p className="text-[10px] text-amber-600 dark:text-amber-400 leading-snug">{speedError}</p>
                              )}
                              <p className="text-[10px] text-muted-foreground leading-snug">
                                {t('Thước = xuất (cùng tốc độ file). Chưa khóa: bấm Áp dụng kể cả 1.00× / 0.80×. Đã khóa cùng số: chọn tốc độ khác rồi Áp dụng. Khớp preferVideo chỉ TTS.', 'Timeline and export use the file speed. Apply even at 1.00× or 0.80× to lock it. If already applied, choose another speed and Apply. Prefer video duration affects only TTS.')}
                              </p>

                              {selected && (
                              <div className="border-t border-border pt-2 space-y-2">
                                <div className="grid grid-cols-2 gap-2">
                                  <NumField
                                    label="Bắt đầu"
                                    value={selected.start}
                                    step={0.1}
                                    disabled={busy}
                                    formatDisplay={formatTimecode}
                                    parseDisplay={parseTimecode}
                                    onCommit={(v) => editSegment({
                                      ...selected,
                                      start: Math.max(prevEnd, Math.min(selected.end - minDur, v)),
                                    })}
                                  />
                                  <NumField
                                    label="Kết thúc"
                                    value={selected.end}
                                    step={0.1}
                                    disabled={busy}
                                    formatDisplay={formatTimecode}
                                    parseDisplay={parseTimecode}
                                    onCommit={(v) => editSegment({
                                      ...selected,
                                      end: Math.min(nextStart, Math.max(selected.start + minDur, v)),
                                    })}
                                  />
                                </div>
                                <PropLabel label="Thời lượng">
                                  <span className="text-xs tabular-nums font-mono">
                                    {formatTimecode(selected.end - selected.start)}
                                  </span>
                                </PropLabel>
                              </div>
                              )}
                            </>
                          )
                        })()}

                        {effectivePropTab === 'audio' && (
                          <>
                            <section className="space-y-2 border-b border-border pb-2" aria-label={t('Thiết lập tách người nói', 'Speaker separation settings')}>
                              <label className="flex cursor-pointer items-center justify-between gap-3 text-xs">
                                <span className="min-w-0"><b className="block text-foreground">{t('Tách người nói', 'Separate speakers')}</b><span className="block pt-0.5 text-[10px] leading-snug text-muted-foreground">{t('Phân vai và dùng giọng riêng cho từng người.', 'Assign roles and an individual voice to each speaker.')}</span></span>
                                <input type="checkbox" className="size-4 shrink-0 accent-primary" checked={Boolean(settings.speakerDiarization)} disabled={busy || settings.engine !== 'whisper'} aria-label={t('Bật tách người nói', 'Enable speaker separation')} onChange={(event) => onSettings({ ...settings, speakerDiarization: event.target.checked })} />
                              </label>
                              {settings.engine !== 'whisper' && <p className="text-[10px] text-amber-700 dark:text-amber-300">{t('Chỉ khả dụng khi nhận dạng bằng Whisper.', 'Available only with Whisper recognition.')}</p>}
                              {settings.speakerDiarization && settings.engine === 'whisper' && (
                                <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2 border-t border-border pt-2 text-[11px]">
                                  <span className="text-foreground">{t('Số người nói', 'Speaker count')}</span>
                                  <select className="h-7 rounded border border-border bg-background px-1.5 text-[11px]" value={settings.speakerCount || 0} disabled={busy} aria-label={t('Số người nói', 'Speaker count')} onChange={(event) => onSettings({ ...settings, speakerCount: Number(event.target.value) })}>
                                    <option value={0}>{t('Tự phát hiện', 'Auto-detect')}</option>
                                    {[2, 3, 4, 5, 6, 7, 8].map((count) => <option key={count} value={count}>{locale === 'en' ? `${count} speakers` : `${count} người`}</option>)}
                                  </select>
                                  <span className="text-foreground">{t('Màu phụ đề theo vai', 'Caption color by role')}</span>
                                  <input type="checkbox" className="size-4 justify-self-end accent-primary" checked={Boolean(settings.speakerCaptionColors)} disabled={busy} aria-label={t('Màu phụ đề theo vai', 'Caption color by role')} onChange={(event) => onSettings({ ...settings, speakerCaptionColors: event.target.checked })} />
                                  <button type="button" className="col-span-2 justify-self-start text-[11px] font-medium text-primary hover:underline" onClick={onOpenProjectSpeakers}>{t('Quản lý vai và giọng →', 'Manage roles and voices →')}</button>
                                </div>
                              )}
                              <p className="text-[10px] leading-snug text-muted-foreground">{t('Đổi thiết lập sẽ áp dụng khi nhận dạng lại.', 'Setting changes apply when recognition runs again.')}</p>
                            </section>
                            <div className="space-y-2 pb-2 border-b border-border">
                              <label className="flex items-center justify-between gap-2 text-xs cursor-pointer">
                                <span className="font-medium text-foreground">Lọc âm thanh gốc</span>
                                <input
                                  type="checkbox"
                                  className="accent-primary"
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
                              {settings.processOriginalAudio && (
                                <>
                                  <div className="flex gap-1" role="radiogroup" aria-label="Chế độ lọc âm gốc">
                                    {(
                                      [
                                        ['no_vocals', 'Xóa lời'],
                                        ['vocals', 'Chỉ giữ lời'],
                                      ] as const
                                    ).map(([value, label]) => (
                                      <button
                                        key={value}
                                        type="button"
                                        className={cn(
                                          'flex-1 rounded-sm border px-1 py-1.5 text-[10px] transition-colors',
                                          settings.originalAudioMode === value
                                            ? 'border-primary text-primary bg-primary/10'
                                            : 'border-border text-muted-foreground hover:text-foreground hover:bg-accent',
                                        )}
                                        disabled={busy}
                                        onClick={() =>
                                          onSettings({ ...settings, originalAudioMode: value })
                                        }
                                      >
                                        {label}
                                      </button>
                                    ))}
                                  </div>
                                  <PropLabel label={`Âm lượng nền: ${Math.max(0, Math.min(200, settings.originalAudioVolume ?? 100))}%`}>
                                    <input
                                      type="range"
                                      min={0}
                                      max={100}
                                      className="w-full accent-primary"
                                      value={Math.max(0, Math.min(200, settings.originalAudioVolume ?? 100))}
                                      disabled={busy || settings.originalAudioMode === 'mute'}
                                      onChange={(e) =>
                                        onSettings({
                                          ...settings,
                                          originalAudioVolume: Math.max(0, Math.min(200, Number(e.target.value) || 0)),
                                        })
                                      }
                                    />
                                  </PropLabel>
                                  {wantNoVocals && stemStatus === 'loading' && (
                                    <p className="text-[10px] text-muted-foreground leading-snug">
                                      Đang tách stem xóa lời {Math.max(1, Math.min(99, stemProgress))}% (Demucs).
                                      Lần đầu có thể cài PyTorch — chờ đến khi cột «Âm gốc» hiện «Xóa lời».
                                    </p>
                                  )}
                                  {wantNoVocals && stemStatus === 'ready' && (
                                    <p className="text-[10px] text-emerald-600 dark:text-emerald-400 leading-snug">
                                      Preview = xuất: video đã mute, phát nền đã xóa lời (+ TTS nếu có).
                                    </p>
                                  )}
                                  {wantNoVocals && stemStatus === 'error' && (
                                    <div className="space-y-1.5">
                                      <p className="text-[10px] text-destructive leading-snug">
                                        Lỗi tách stem: {stemError || 'không rõ'} — tạm mute âm gốc (tránh còn lời).
                                      </p>
                                      <button
                                        type="button"
                                        className="w-full rounded-md border border-border bg-accent hover:bg-muted px-2 py-1.5 text-[11px] transition-colors"
                                        disabled={busy}
                                        onClick={() => setStemRetry((n) => n + 1)}
                                      >
                                        Thử tách lại
                                      </button>
                                    </div>
                                  )}
                                  {settings.originalAudioMode === 'vocals' && (
                                    <p className="text-[10px] text-muted-foreground leading-snug">
                                      «Chỉ giữ lời» áp dụng khi xuất (preview vẫn nghe bản gốc).
                                    </p>
                                  )}
                                </>
                              )}
                              {!settings.processOriginalAudio && (
                                <p className="text-[10px] text-muted-foreground leading-snug">
                                  Chưa bật lọc: bản xuất vẫn trộn âm gốc (nhạc/lời) dưới TTS — xem cột «Âm gốc».
                                </p>
                              )}
                            </div>

                            {/* Clip lồng tiếng + giọng — 1 đoạn hoặc tất cả */}
                            <div className="space-y-2 pb-2 border-b border-border">
                              {selected && speakerProfiles.length > 0 && (
                                <PropLabel label={t('Người nói', 'Speaker')}>
                                  <div className="flex min-w-0 items-center gap-1.5">
                                    <span className="size-3 shrink-0 rounded-full border border-white/50" style={{ background: selectedSpeaker?.color || 'var(--muted)' }} />
                                    <select
                                      className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-xs"
                                      value={selected.speaker || ''}
                                      disabled={busy}
                                      onChange={(event) => {
                                        const profile = speakerProfiles.find((item) => item.id === event.target.value)
                                        void editSegment({
                                          ...selected,
                                          speaker: event.target.value,
                                          ...(profile?.voice ? { voice: profile.voice } : {}),
                                          audioFile: undefined,
                                          audioUrl: undefined,
                                          audioDuration: undefined,
                                        })
                                      }}
                                    >
                                      <option value="">{t('Chưa gán', 'Unassigned')}</option>
                                      {speakerProfiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
                                    </select>
                                  </div>
                                </PropLabel>
                              )}
                              <PropLabel label="Clip lồng tiếng">
                                <span className="text-xs text-muted-foreground">
                                  {selected
                                    ? `#${String(selected.index).padStart(2, '0')} · ${(selected.audioDuration ?? 0).toFixed(2)}s · slot ${(selected.end - selected.start).toFixed(2)}s`
                                    : settings.speakerDiarization
                                      ? `${t('Theo vai', 'By role')} · ${speakerProfiles.length} ${t('vai', 'roles')} · ${segments.filter((s) => segmentHasDub(s)).length} ${t('đoạn TTS', 'TTS segments')}`
                                      : `${t('Tất cả', 'All')} · ${segments.filter((s) => segmentHasDub(s)).length} ${t('đoạn bật TTS', 'segments with TTS enabled')}`}
                                </span>
                              </PropLabel>
                              <PropLabel label={settings.speakerDiarization && !selected ? t('Giọng đọc theo vai', 'Voice by role') : t('Giọng đọc', 'Voice')}>
                                <div className="flex gap-1.5 items-stretch">
                                  {settings.speakerDiarization && !selected ? (
                                    <div className="flex w-full gap-1.5">
                                      <button type="button" className="min-w-0 flex-1 rounded-md border border-border px-2 py-1.5 text-[11px] font-medium text-primary hover:bg-accent" onClick={onOpenProjectSpeakers}>{t('Quản lý vai/giọng', 'Manage roles/voices')}</button>
                                      <button type="button" className="shrink-0 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1.5 text-[11px] font-medium text-primary hover:bg-primary/20 disabled:opacity-50" disabled={busy || !onDub || segments.length === 0} title={t('Tạo TTS bằng giọng đã gán cho từng vai', 'Generate TTS using the voice assigned to each role')} onClick={() => void onDub?.()}>{busy && jobStep === 'dub' ? `${Math.round(jobProgress || 0)}%` : t('Tạo TTS theo vai', 'Generate TTS by role')}</button>
                                    </div>
                                  ) : <>
                                  <select
                                    className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-xs"
                                    value={
                                      selected
                                        ? (selected.voice || settings.defaultVoice || globalVoice)
                                        : (globalVoice || settings.defaultVoice || '')
                                    }
                                    disabled={busy}
                                    onChange={(e) => {
                                      const v = e.target.value
                                      setGlobalVoice(v)
                                      if (selected) {
                                        editSegment({
                                          ...selected,
                                          voice: v,
                                          ...(isOcrOverlayLayout(selected.layout) ? { dub: true } : {}),
                                        })
                                      } else {
                                        pushHistory()
                                        const applied = segments.map((s) => {
                                          if ((s.layout === 'vertical' || s.layout === 'label') && s.dub !== true) return s
                                          return { ...s, voice: v }
                                        })
                                        void onSegmentsReplace(applied)
                                        if (v) onSettings({ ...settings, defaultVoice: v })
                                      }
                                    }}
                                  >
                                    {voices.map((v) => (
                                      <option key={v.id} value={v.id}>{v.name}</option>
                                    ))}
                                  </select>
                                  {selected?.speaker && (
                                    <button
                                      type="button"
                                      className="shrink-0 rounded-md border border-border px-2 py-1.5 text-[11px] hover:bg-accent disabled:opacity-50"
                                      disabled={busy}
                                      title={locale === 'en' ? `Assign this voice to every ${selectedSpeaker?.name || selected.speaker} segment` : `Gán giọng này cho mọi đoạn của ${selectedSpeaker?.name || selected.speaker}`}
                                      onClick={() => {
                                        const voice = selected.voice || settings.defaultVoice
                                        const speaker = selected.speaker
                                        if (!speaker || !voice) return
                                        pushHistory()
                                        void onSegmentsReplace(segments.map((s) => s.speaker === speaker ? {
                                          ...s, voice, audioFile: undefined, audioUrl: undefined, audioDuration: undefined,
                                        } : s))
                                        onSettings({
                                          ...settings,
                                          speakerVoices: { ...(settings.speakerVoices || {}), [speaker]: voice },
                                          speakerProfiles: {
                                            ...(settings.speakerProfiles || {}),
                                            [speaker]: {
                                              id: speaker,
                                              name: selectedSpeaker?.name || speaker,
                                              color: selectedSpeaker?.color || '#0ea5a8',
                                              voice,
                                            },
                                          },
                                        })
                                        if (onDub) queueMicrotask(() => void onDub())
                                      }}
                                    >
                                      {t('Áp dụng cho vai', 'Apply to speaker')}
                                    </button>
                                  )}
                                  {!selected && (
                                    <button
                                      type="button"
                                      className="shrink-0 rounded-md border border-primary/40 bg-primary/10 text-primary hover:bg-primary/20 px-2.5 py-1.5 text-[11px] font-medium transition-colors disabled:opacity-50 whitespace-nowrap"
                                      disabled={busy || !onDub || segments.length === 0}
                                      title="Gán giọng + volume/tốc độ rồi lồng tiếng toàn bộ"
                                      onClick={() => {
                                        pushHistory()
                                        const vol = Math.max(0, Math.min(200, globalTtsVolume))
                                        const sp = Math.max(0.75, Math.min(1.5, globalTtsSpeed))
                                        const voice = globalVoice || settings.defaultVoice
                                        const applied = segments.map((s) => {
                                          if ((s.layout === 'vertical' || s.layout === 'label') && s.dub !== true) {
                                            return s
                                          }
                                          return {
                                            ...s,
                                            ttsVolume: vol,
                                            ttsSpeed: sp,
                                            voice,
                                            audioFile: undefined,
                                            audioUrl: undefined,
                                            audioDuration: undefined,
                                          }
                                        })
                                        void onSegmentsReplace(applied)
                                        if (voice) onSettings({ ...settings, defaultVoice: voice })
                                        window.setTimeout(() => onDub?.(), 150)
                                      }}
                                    >
                                      {busy && jobStep === 'dub'
                                        ? `${Math.round(jobProgress || 0)}%`
                                        : 'Tạo TTS tất cả'}
                                    </button>
                                  )}
                                  </>}
                                </div>
                              </PropLabel>
                              {selected && segmentHasDub(selected) && (
                                <div className="flex gap-1">
                                  <button
                                    type="button"
                                    className="flex-1 rounded-md border border-border bg-accent hover:bg-muted px-2 py-1.5 text-xs transition-colors disabled:opacity-50"
                                    disabled={busy || !selected.audioUrl}
                                    onClick={() => playSegmentDub(selected)}
                                  >
                                    Phát với timeline
                                  </button>
                                  <button
                                    type="button"
                                    className="flex-1 rounded-md border border-border bg-accent hover:bg-muted px-2 py-1.5 text-xs transition-colors disabled:opacity-50"
                                    disabled={busy || ttsBusy || !selected.translation.trim()}
                                    onClick={() => void previewTts()}
                                  >
                                    {ttsBusy ? 'Đang tạo…' : 'Tạo lại TTS'}
                                  </button>
                                </div>
                              )}
                              {selected && !segmentHasDub(selected) && (
                                <p className="text-[11px] text-muted-foreground leading-relaxed">
                                  Đoạn tắt lồng tiếng — bật ở tab Phụ đề.
                                </p>
                              )}
                            </div>

                            {selected ? (
                              <>
                            <PropLabel label={t(`Âm lượng TTS: ${selected.ttsVolume ?? 100}%`, `TTS volume: ${selected.ttsVolume ?? 100}%`)}>
                              <input type="range" min={0} max={200}
                                className="w-full accent-primary"
                                value={selected.ttsVolume ?? 100}
                                onChange={(e) => editSegment(
                                  { ...selected, ttsVolume: Number(e.target.value) },
                                  { textField: 'ttsVolume' },
                                )}
                              />
                            </PropLabel>
                            <div className="flex gap-1">
                              {[0, 50, 100, 150, 200].map((v) => (
                                <button
                                  key={v}
                                  type="button"
                                  className={cn(
                                    'flex-1 rounded-sm border px-1 py-1 text-[10px] transition-colors',
                                    (selected.ttsVolume ?? 100) === v
                                      ? 'border-primary text-primary bg-primary/10'
                                      : 'border-border text-muted-foreground hover:text-foreground hover:bg-accent',
                                  )}
                                  onClick={() => editSegment({ ...selected, ttsVolume: v })}
                                >
                                  {v === 0 ? 'Tắt' : `${v}%`}
                                </button>
                              ))}
                            </div>

                            {(() => {
                              const segRatio = ttsPlayRatio(selected.ttsBake, bakedSpeed)
                              const eff = (selected.ttsSpeed ?? 1) * segRatio
                              return (
                                <>
                                  <PropLabel label={Math.abs(segRatio - 1) > 0.02
                                    ? t(`Tốc độ TTS (phát thực): ${eff.toFixed(2)}× — dub ở ${(selected.ttsBake ?? 1).toFixed(2)}, timeline ${(bakedSpeed ?? 1).toFixed(2)}`, `TTS speed (playback): ${eff.toFixed(2)}× — dubbed at ${(selected.ttsBake ?? 1).toFixed(2)}, timeline ${(bakedSpeed ?? 1).toFixed(2)}`)
                                    : t(`Tốc độ TTS: ${eff.toFixed(2)}×`, `TTS speed: ${eff.toFixed(2)}×`)}>
                                    <input type="range"
                                      min={0.75 * segRatio} max={1.5 * segRatio} step={0.05}
                                      className="w-full accent-primary"
                                      value={eff}
                                      onChange={(e) => editSegment(
                                        { ...selected, ttsSpeed: clampTtsManual(Number(e.target.value) / segRatio) },
                                        { textField: 'ttsSpeed' },
                                      )}
                                    />
                                  </PropLabel>
                                  <div className="flex gap-1">
                                    {TTS_PRESETS.map((v) => {
                                      const manual = v / segRatio
                                      const ok = manual >= 0.749 && manual <= 1.501
                                      return (
                                        <button
                                          key={v}
                                          type="button"
                                          disabled={!ok}
                                          title={ok ? undefined : 'Ngoài dải nén cho phép của đồng hồ hiện tại'}
                                          className={cn(
                                            'flex-1 rounded-sm border px-1 py-1 text-[10px] transition-colors',
                                            Math.abs(eff - v) < 0.03
                                              ? 'border-primary text-primary bg-primary/10'
                                              : ok
                                                ? 'border-border text-muted-foreground hover:text-foreground hover:bg-accent'
                                                : 'border-border text-muted-foreground/40',
                                          )}
                                          onClick={() => editSegment({ ...selected, ttsSpeed: clampTtsManual(manual) })}
                                        >
                                          {v}×
                                        </button>
                                      )
                                    })}
                                  </div>
                                </>
                              )
                            })()}

                            <button
                              type="button"
                              className="w-full rounded-md border border-border bg-accent hover:bg-muted px-3 py-1.5 text-xs transition-colors"
                              onClick={() => editSegment({ ...selected, ttsVolume: 100, ttsSpeed: 1 })}
                            >
                              Reset âm thanh mặc định
                            </button>
                              </>
                            ) : (
                              <>
                                <p className="text-[11px] text-muted-foreground leading-relaxed">
                                  Chọn giọng (nút <strong className="text-foreground font-medium">Tạo TTS tất cả</strong> bên phải) hoặc kéo thanh chỉnh volume/tốc độ để áp dụng cho tất cả các đoạn.
                                </p>
                                <PropLabel label={t(`Âm lượng TTS: ${globalTtsVolume}% · tất cả`, `TTS volume: ${globalTtsVolume}% · all`)}>
                                  <input
                                    type="range"
                                    min={0}
                                    max={200}
                                    className="w-full accent-primary"
                                    value={globalTtsVolume}
                                    disabled={busy}
                                    onChange={(e) => setGlobalTtsVolume(Number(e.target.value))}
                                    onPointerUp={(e) => {
                                      const v = Number(e.currentTarget.value)
                                      pushHistory()
                                      const applied = segments.map((s) => {
                                        if ((s.layout === 'vertical' || s.layout === 'label') && s.dub !== true) return s
                                        return { ...s, ttsVolume: v }
                                      })
                                      void onSegmentsReplace(applied)
                                    }}
                                  />
                                </PropLabel>
                                <div className="flex gap-1">
                                  {[0, 50, 100, 150, 200].map((v) => (
                                    <button
                                      key={v}
                                      type="button"
                                      className={cn(
                                        'flex-1 rounded-sm border px-1 py-1 text-[10px] transition-colors',
                                        globalTtsVolume === v
                                          ? 'border-primary text-primary bg-primary/10'
                                          : 'border-border text-muted-foreground hover:text-foreground hover:bg-accent',
                                      )}
                                      disabled={busy}
                                      onClick={() => {
                                        setGlobalTtsVolume(v)
                                        pushHistory()
                                        const applied = segments.map((s) => {
                                          if ((s.layout === 'vertical' || s.layout === 'label') && s.dub !== true) return s
                                          return { ...s, ttsVolume: v }
                                        })
                                        void onSegmentsReplace(applied)
                                      }}
                                    >
                                      {v === 0 ? 'Tắt' : `${v}%`}
                                    </button>
                                  ))}
                                </div>

                                {(() => {
                                  const dubRef = segments.find((s) => (s.ttsBake ?? 0) > 0)
                                  const allRatio = ttsPlayRatio(dubRef?.ttsBake, bakedSpeed)
                                  const effAll = globalTtsSpeed * allRatio
                                  const applyManual = (manual: number) => {
                                    const m = clampTtsManual(manual)
                                    setGlobalTtsSpeed(m)
                                    pushHistory()
                                    const applied = segments.map((s) => {
                                      if ((s.layout === 'vertical' || s.layout === 'label') && s.dub !== true) return s
                                      return { ...s, ttsSpeed: m }
                                    })
                                    void onSegmentsReplace(applied)
                                  }
                                  return (
                                    <>
                                      <PropLabel label={Math.abs(allRatio - 1) > 0.02
                                        ? t(`Tốc độ TTS (phát thực): ${effAll.toFixed(2)}× · tất cả — dub ở ${(dubRef?.ttsBake ?? 1).toFixed(2)}, timeline ${(bakedSpeed ?? 1).toFixed(2)}`, `TTS speed (playback): ${effAll.toFixed(2)}× · all — dubbed at ${(dubRef?.ttsBake ?? 1).toFixed(2)}, timeline ${(bakedSpeed ?? 1).toFixed(2)}`)
                                        : t(`Tốc độ TTS: ${effAll.toFixed(2)}× · tất cả`, `TTS speed: ${effAll.toFixed(2)}× · all`)}>
                                        <input
                                          type="range"
                                          min={0.75 * allRatio}
                                          max={1.5 * allRatio}
                                          step={0.05}
                                          className="w-full accent-primary"
                                          value={effAll}
                                          disabled={busy}
                                          onChange={(e) => setGlobalTtsSpeed(clampTtsManual(Number(e.target.value) / allRatio))}
                                          onPointerUp={(e) => applyManual(Number(e.currentTarget.value) / allRatio)}
                                        />
                                      </PropLabel>
                                      <div className="flex gap-1">
                                        {TTS_PRESETS.map((v) => {
                                          const manual = v / allRatio
                                          const ok = manual >= 0.749 && manual <= 1.501
                                          return (
                                            <button
                                              key={v}
                                              type="button"
                                              disabled={busy || !ok}
                                              title={ok ? undefined : 'Ngoài dải nén cho phép của đồng hồ hiện tại'}
                                              className={cn(
                                                'flex-1 rounded-sm border px-1 py-1 text-[10px] transition-colors',
                                                Math.abs(effAll - v) < 0.03
                                                  ? 'border-primary text-primary bg-primary/10'
                                                  : ok
                                                    ? 'border-border text-muted-foreground hover:text-foreground hover:bg-accent'
                                                    : 'border-border text-muted-foreground/40',
                                              )}
                                              onClick={() => applyManual(manual)}
                                            >
                                              {v}×
                                            </button>
                                          )
                                        })}
                                      </div>
                                    </>
                                  )
                                })()}

                                <button
                                  type="button"
                                  className="w-full rounded-md border border-border bg-accent hover:bg-muted px-3 py-1.5 text-xs transition-colors"
                                  disabled={busy}
                                  onClick={() => {
                                    setGlobalTtsVolume(100)
                                    setGlobalTtsSpeed(1)
                                    setGlobalVoice(settings.defaultVoice || '')
                                    pushHistory()
                                    const applied = segments.map((s) => {
                                      if ((s.layout === 'vertical' || s.layout === 'label') && s.dub !== true) return s
                                      return { ...s, ttsVolume: 100, ttsSpeed: 1, voice: settings.defaultVoice || '' }
                                    })
                                    void onSegmentsReplace(applied)
                                  }}
                                >
                                  Reset về 100% · 1× · giọng mặc định
                                </button>
                              </>
                            )}
                          </>
                        )}

                        {effectivePropTab === 'mask' && selectedIsWatermark && selectedOverlay && (
                          <section className="space-y-3" aria-label="Watermark bbox">
                            <div className="rounded-md border border-teal-400/40 bg-teal-500/5 p-2 text-[11px] leading-relaxed text-muted-foreground">
                              <p className="font-medium text-foreground">{t('Vùng che logo', 'Logo mask')}</p>
                              <p>{t('Khung này chỉ áp dụng cho logo/OCR đang chọn. Không làm thay đổi bbox của phụ đề.', 'This box only applies to the selected logo/OCR. It does not change the caption bounding box.')}</p>
                            </div>
                            <div className="grid grid-cols-2 gap-2">
                              <NumField label="X" value={selectedOverlay.x} disabled={busy} onCommit={(x) => editOverlay({ ...selectedOverlay, x: Math.round(Math.max(0, Math.min(sourceWidth - selectedOverlay.w, x))) })} />
                              <NumField label="Y" value={selectedOverlay.y} disabled={busy} onCommit={(y) => editOverlay({ ...selectedOverlay, y: Math.round(Math.max(0, Math.min(sourceHeight - selectedOverlay.h, y))) })} />
                              <NumField label="Rộng" value={selectedOverlay.w} disabled={busy} onCommit={(w) => editOverlay({ ...selectedOverlay, w: Math.round(Math.max(12, Math.min(sourceWidth - selectedOverlay.x, w))) })} />
                              <NumField label="Cao" value={selectedOverlay.h} disabled={busy} onCommit={(h) => editOverlay({ ...selectedOverlay, h: Math.round(Math.max(12, Math.min(sourceHeight - selectedOverlay.y, h))) })} />
                            </div>
                            <div className="grid grid-cols-2 gap-2">
                              <NumField label="Bắt đầu" value={selectedOverlay.start} disabled={busy} step={0.1} formatDisplay={formatTimecode} parseDisplay={parseTimecode} onCommit={(start) => editOverlay({ ...selectedOverlay, start: Math.max(0, Math.min(selectedOverlay.end - 0.04, start)) })} />
                              <NumField label="Kết thúc" value={selectedOverlay.end} disabled={busy} step={0.1} formatDisplay={formatTimecode} parseDisplay={parseTimecode} onCommit={(end) => editOverlay({ ...selectedOverlay, end: Math.min(timelineDuration, Math.max(selectedOverlay.start + 0.04, end)) })} />
                            </div>
                            <button type="button" className="w-full rounded-md border border-destructive/50 px-3 py-2 text-xs text-destructive hover:bg-destructive/10" disabled={busy} onClick={() => { onOverlayDelete(selectedOverlay.id); setSelectedOverlayId(null) }}>
                              {t('Xóa vùng che logo', 'Delete logo mask')}
                            </button>
                          </section>
                        )}

                        {effectivePropTab === 'mask' && !selectedIsWatermark && (
                            <EditorMaskPanel
                            busy={busy}
                            settings={settings}
                              onSettings={onSettings}
                              onPreviewCoverMaskOpacity={onPreviewCoverMaskOpacity}
                            coverMaskStyle={coverMaskStyle}
                            coverMaskColor={coverMaskColor}
                            coverMaskOpacity={coverMaskOpacity}
                            selected={selected}
                            bboxSeg={bboxSeg}
                            selectedBox={selectedBox}
                            sourceWidth={sourceWidth}
                            sourceHeight={sourceHeight}
                            segmentsLen={segments.length}
                            timelineDuration={timelineDuration}
                            playheadSec={playheadSec}
                            commitCoverBox={commitCoverBox}
                            stretchCoverFullWidth={stretchCoverFullWidth}
                            applyCoverMaskToAll={applyCoverMaskToAll}
                            resetOcrRegion={resetOcrRegion}
                            applyAllLaneLabel={applyAllLaneLabel}
                          />
                        )}

                        {effectivePropTab === 'overlay' && logoDraft && (
                          <div className="logo-editor-panel">
                            <div className="logo-editor-full"><p className="text-sm font-medium">Logo / Watermark</p><p className="text-[10px] text-muted-foreground">Bản xem trước · chưa lưu vào video</p></div>
                            {logoDraft.logoSource === 'text' && <div className="space-y-2"><PropLabel label="Nội dung"><input className="w-full rounded-md border border-border bg-input px-2 py-2 text-xs outline-none focus:border-primary" value={logoDraft.text} onChange={(e) => setLogoDraft(fitTextLogo(logoDraft, e.target.value))} /></PropLabel><div className="grid grid-cols-[1fr_auto] gap-2"><PropLabel label="Phông chữ"><select className="w-full rounded-md border border-border bg-input px-2 py-1.5 text-xs outline-none focus:border-primary" value={logoDraft.fontFamily ?? 'system'} onChange={(e) => { const next = { ...logoDraft, fontFamily: e.target.value }; setLogoDraft(fitTextLogo(next)) }}>{CAPTION_FONT_PRESETS.map((font) => <option key={font.id} value={font.id} style={{ fontFamily: font.css }}>{font.label}</option>)}</select></PropLabel><PropLabel label="Màu chữ"><input type="color" className="h-8 w-14 cursor-pointer rounded-md border border-border bg-input" value={logoDraft.color} onChange={(e) => setLogoDraft({ ...logoDraft, color: e.target.value })} /></PropLabel></div><div className="flex gap-1">{['#ffffff', '#000000', '#ffd166', '#ef476f', '#06d6a0', '#118ab2'].map((color) => <button key={color} type="button" title={color} className={cn('size-5 rounded-full border transition-transform hover:scale-110', logoDraft.color === color ? 'border-primary ring-1 ring-primary' : 'border-border')} style={{ backgroundColor: color }} onClick={() => setLogoDraft({ ...logoDraft, color })} />)}</div></div>}
                            <div className={cn('rounded-md border border-border p-2 space-y-2', logoDraft.logoSource !== 'text' && 'logo-editor-full')}>
                              <PropLabel label={`Kích thước: ${logoDraft.logoSource === 'text' ? `${logoDraft.fontSize}px` : `${Math.round(logoDraft.h / Math.max(1, Math.min(sourceWidth, sourceHeight)) * 100)}%`}`}>
                                <input type="range" min={logoDraft.logoSource === 'text' ? 6 : 2} max={logoDraft.logoSource === 'text' ? 160 : 30} value={logoDraft.logoSource === 'text' ? logoDraft.fontSize : Math.round(logoDraft.h / Math.max(1, Math.min(sourceWidth, sourceHeight)) * 100)} className="w-full accent-primary" onChange={(e) => {
                                  const value = Number(e.target.value)
                                  if (logoDraft.logoSource === 'text') setLogoDraft(fitTextLogo(logoDraft, logoDraft.text, value))
                                  else { const ratio = logoDraft.w / Math.max(1, logoDraft.h); const h = Math.round(Math.min(sourceWidth, sourceHeight) * value / 100); setLogoDraft({ ...logoDraft, h, w: Math.round(h * ratio) }) }
                                }} />
                              </PropLabel>
                              <details open><summary className="cursor-pointer text-[10px] text-muted-foreground hover:text-foreground">Nâng cao</summary><div className="mt-2 grid grid-cols-4 gap-1.5"><NumField label="Rộng" value={logoDraft.w} onCommit={(v) => setLogoDraft({ ...logoDraft, w: Math.max(20, Math.round(v)) })} /><NumField label="Cao" value={logoDraft.h} onCommit={(v) => setLogoDraft({ ...logoDraft, h: Math.max(20, Math.round(v)) })} /><NumField label="X" value={logoDraft.x} onCommit={(v) => setLogoDraft({ ...logoDraft, x: Math.max(0, Math.min(sourceWidth - logoDraft.w, Math.round(v))) })} /><NumField label="Y" value={logoDraft.y} onCommit={(v) => setLogoDraft({ ...logoDraft, y: Math.max(0, Math.min(sourceHeight - logoDraft.h, Math.round(v))) })} /></div></details>
                            </div>
                            <div className="col-span-2 rounded-md border border-border p-2 space-y-2">
                              <p className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Vị trí / chuyển động</p>
                              <div className="grid grid-cols-2 gap-1">{(['fixed', 'random'] as const).map((motion) => <button key={motion} type="button" className={cn('rounded-md border p-1.5 text-xs transition-colors hover:bg-primary/10', logoDraft.motion === motion ? 'border-primary bg-primary/15 text-primary ring-1 ring-primary' : 'border-border')} onClick={() => setLogoDraft({ ...logoDraft, motion })}>{logoDraft.motion === motion ? '✓ ' : ''}{motion === 'fixed' ? 'Cố định' : 'Ngẫu nhiên'}</button>)}</div>
                              <div className="grid grid-cols-2 gap-1">{(['full', 'range'] as const).map((scope) => <button key={scope} type="button" className={cn('rounded-md border p-1.5 text-xs transition-colors hover:bg-primary/10', logoDraft.scope === scope ? 'border-primary bg-primary/15 text-primary ring-1 ring-primary' : 'border-border')} onClick={() => setLogoDraft({ ...logoDraft, scope, ...(scope === 'full' ? { start: 0, end: timelineDuration } : {}) })}>{logoDraft.scope === scope ? '✓ ' : ''}{scope === 'full' ? 'Toàn video' : 'Theo đoạn'}</button>)}</div>
                              <PropLabel label={`Độ mờ: ${logoDraft.opacity ?? 85}%`}><input type="range" min={5} max={100} value={logoDraft.opacity ?? 85} className="w-full accent-primary" onChange={(e) => setLogoDraft({ ...logoDraft, opacity: Number(e.target.value) })} /></PropLabel>
                              {logoDraft.scope === 'range' && (
                                <div className="grid grid-cols-2 gap-2">
                                  <NumField
                                    label="Hiện từ"
                                    value={logoDraft.start}
                                    step={0.1}
                                    formatDisplay={formatTimecode}
                                    parseDisplay={parseTimecode}
                                    onCommit={(v) => setLogoDraft({ ...logoDraft, start: Math.max(0, Math.min(logoDraft.end - 0.1, v)) })}
                                  />
                                  <NumField
                                    label="Đến"
                                    value={logoDraft.end}
                                    step={0.1}
                                    formatDisplay={formatTimecode}
                                    parseDisplay={parseTimecode}
                                    onCommit={(v) => setLogoDraft({ ...logoDraft, end: Math.min(timelineDuration, Math.max(logoDraft.start + 0.1, v)) })}
                                  />
                                </div>
                              )}
                              {logoDraft.motion === 'random' && <div className="grid grid-cols-[auto_1fr_2fr] items-end gap-2"><p className="pb-2 text-[10px] font-medium text-muted-foreground">Tốc độ</p><div className="grid grid-cols-3 gap-1">{[
                                { label: 'Chậm', visibleSec: 6, hiddenSec: 3 },
                                { label: 'Vừa', visibleSec: 4, hiddenSec: 2 },
                                { label: 'Nhanh', visibleSec: 2.5, hiddenSec: 1 },
                              ].map((preset) => { const active = logoDraft.visibleSec === preset.visibleSec && logoDraft.hiddenSec === preset.hiddenSec; return <button key={preset.label} type="button" className={cn('rounded-md border p-1.5 text-[10px] transition-colors hover:bg-primary/10', active ? 'border-primary bg-primary/15 text-primary ring-1 ring-primary' : 'border-border')} onClick={() => setLogoDraft({ ...logoDraft, visibleSec: preset.visibleSec, hiddenSec: preset.hiddenSec })}>{active ? '✓ ' : ''}{preset.label}</button> })}</div><div className="grid grid-cols-4 gap-1.5"><NumField label="Hiện" value={logoDraft.visibleSec ?? 4} step={0.1} formatDisplay={formatTimecode} parseDisplay={parseTimecode} onCommit={(v) => setLogoDraft({ ...logoDraft, visibleSec: Math.max(0.5, v) })} /><NumField label="Ẩn" value={logoDraft.hiddenSec ?? 2} step={0.1} formatDisplay={formatTimecode} parseDisplay={parseTimecode} onCommit={(v) => setLogoDraft({ ...logoDraft, hiddenSec: Math.max(0, v) })} /><NumField label="Fade" value={logoDraft.fadeSec ?? 0.5} step={0.1} formatDisplay={formatTimecode} parseDisplay={parseTimecode} onCommit={(v) => setLogoDraft({ ...logoDraft, fadeSec: Math.max(0, v) })} /><NumField label="Lề (%)" value={logoDraft.safeMargin ?? 4} step={1} onCommit={(v) => setLogoDraft({ ...logoDraft, safeMargin: Math.max(0, Math.min(20, v)) })} /></div></div>}
                            </div>
                            {logoError && <p className="col-span-2 text-[10px] text-destructive">{logoError}</p>}
                            <button type="button" disabled={logoToggleDisabled} className={cn('col-span-2 w-full rounded-md px-3 py-2 text-xs font-medium transition-colors disabled:opacity-50', logoToggleRemoves ? 'border border-destructive/50 text-destructive hover:bg-destructive/10' : 'bg-primary text-primary-foreground hover:bg-primary/90')} onClick={() => logoToggleRemoves ? unapplyLogo() : void applyLogoDraft()}>{logoApplying ? 'Đang áp dụng…' : logoToggleRemoves ? 'Hủy áp dụng logo' : 'Áp dụng logo'}</button>
                          </div>
                        )}

                        {effectivePropTab === 'overlay' && selectedOverlay?.kind === 'logo' && !logoDraft && (
                          <button type="button" className="w-full rounded-md border border-destructive/50 px-3 py-2 text-xs text-destructive hover:bg-destructive/10" onClick={unapplyLogo}>Hủy áp dụng logo</button>
                        )}

                        {effectivePropTab === 'overlay' && selectedOverlay?.kind === 'effect' && !logoDraft && (
                          <>
                            <div className="rounded-md border border-fuchsia-400/40 bg-fuchsia-500/5 px-2.5 py-2 text-xs text-foreground">
                              <b>{t('Vùng hiệu ứng', 'Effect region')}</b>
                              <p className="mt-1 text-[11px] text-muted-foreground">{t('Kéo giữa khung trên video để di chuyển; rê sát 4 góc hoặc 4 cạnh để co giãn.', 'Drag inside the frame on video to move it; hover near its 4 corners or 4 edges to resize.')}</p>
                            </div>
                            <div className="grid grid-cols-2 gap-2">
                              <NumField label={t('Hiện từ', 'Start')} value={selectedOverlay.start} step={0.1} formatDisplay={formatTimecode} parseDisplay={parseTimecode} onCommit={(v) => editOverlay({ ...selectedOverlay, start: Math.max(0, Math.min(selectedOverlay.end - 0.1, v)) })} />
                              <NumField label={t('Đến', 'End')} value={selectedOverlay.end} step={0.1} formatDisplay={formatTimecode} parseDisplay={parseTimecode} onCommit={(v) => editOverlay({ ...selectedOverlay, end: Math.min(timelineDuration, Math.max(selectedOverlay.start + 0.1, v)) })} />
                              <NumField label="X" value={selectedOverlay.x} onCommit={(v) => editOverlay({ ...selectedOverlay, x: Math.round(Math.max(0, Math.min(sourceWidth - selectedOverlay.w, v))) })} />
                              <NumField label="Y" value={selectedOverlay.y} onCommit={(v) => editOverlay({ ...selectedOverlay, y: Math.round(Math.max(0, Math.min(sourceHeight - selectedOverlay.h, v))) })} />
                              <NumField label={t('Rộng', 'Width')} value={selectedOverlay.w} onCommit={(v) => editOverlay({ ...selectedOverlay, w: Math.round(Math.max(20, Math.min(sourceWidth - selectedOverlay.x, v))) })} />
                              <NumField label={t('Cao', 'Height')} value={selectedOverlay.h} onCommit={(v) => editOverlay({ ...selectedOverlay, h: Math.round(Math.max(20, Math.min(sourceHeight - selectedOverlay.y, v))) })} />
                            </div>
                            <PropLabel label={t('Kiểu hiệu ứng', 'Effect style')}>
                              <select className="h-8 w-full rounded border border-border bg-background px-2 text-xs" value={selectedOverlay.maskStyle ?? 'blur'} onChange={(e) => editOverlay({ ...selectedOverlay, maskStyle: e.target.value as NonNullable<TextOverlay['maskStyle']> })}>
                                <option value="blur">{t('Làm mờ', 'Blur')}</option><option value="feather">{t('Mờ tan mép', 'Feathered blur')}</option><option value="solid">{t('Màu nền', 'Solid')}</option><option value="mosaic">{t('Khối', 'Mosaic')}</option>
                              </select>
                            </PropLabel>
                            <div className="grid grid-cols-[auto_1fr] items-center gap-2 rounded-md border border-border p-2">
                              <input type="color" aria-label={t('Màu vùng hiệu ứng', 'Effect color')} className="h-8 w-12 cursor-pointer rounded border border-border bg-input p-1" value={selectedOverlay.maskColor ?? '#4c1d95'} onChange={(e) => editOverlay({ ...selectedOverlay, maskColor: e.target.value })} />
                            <PropLabel label={`${t('Độ đậm', 'Opacity')}: ${effectOpacityDraft ?? selectedOverlay.maskOpacity ?? 0}%`}><input type="range" min={0} max={100} className="w-full accent-primary" value={effectOpacityDraft ?? selectedOverlay.maskOpacity ?? 0} onInput={(e) => previewEffectOpacity(Number(e.currentTarget.value))} onPointerUp={commitEffectOpacity} onPointerCancel={commitEffectOpacity} onBlur={commitEffectOpacity} onKeyUp={commitEffectOpacity} /></PropLabel>
                            </div>
                            <button type="button" className="w-full rounded-md border border-border bg-accent hover:bg-muted px-3 py-1.5 text-xs transition-colors" onClick={() => editOverlay({ ...selectedOverlay, id: crypto.randomUUID(), start: Math.min(timelineDuration - 0.1, selectedOverlay.end), end: Math.min(timelineDuration, selectedOverlay.end + (selectedOverlay.end - selectedOverlay.start)) }, true)}>{t('Nhân bản vùng hiệu ứng', 'Duplicate effect region')}</button>
                            <button type="button" className="w-full rounded-md border border-destructive/50 text-destructive hover:bg-destructive/10 px-3 py-1.5 text-xs transition-colors" onClick={() => { onOverlayDelete(selectedOverlay.id); setSelectedOverlayId(null) }}>{t('Xóa vùng hiệu ứng', 'Delete effect region')}</button>
                          </>
                        )}
                        {effectivePropTab === 'overlay' && selectedOverlay && selectedOverlay.kind !== 'logo' && selectedOverlay.kind !== 'effect' && !logoDraft && (
                          <>
                            <PropLabel label="Nội dung">
                              <textarea
                                className="w-full rounded-md border border-border bg-input px-2 py-1.5 text-xs resize-none outline-none focus:border-ring transition-colors"
                                value={selectedOverlay.text}
                                rows={3}
                                onChange={(e) => editOverlay({ ...selectedOverlay, text: e.target.value })}
                              />
                            </PropLabel>

                            <div className="grid grid-cols-2 gap-2">
                              <NumField
                                label="Hiện từ"
                                value={selectedOverlay.start}
                                step={0.1}
                                formatDisplay={formatTimecode}
                                parseDisplay={parseTimecode}
                                onCommit={(v) => editOverlay({ ...selectedOverlay, start: Math.max(0, Math.min(selectedOverlay.end - 0.1, v)) })}
                              />
                              <NumField
                                label="Đến"
                                value={selectedOverlay.end}
                                step={0.1}
                                formatDisplay={formatTimecode}
                                parseDisplay={parseTimecode}
                                onCommit={(v) => editOverlay({ ...selectedOverlay, end: Math.min(timelineDuration, Math.max(selectedOverlay.start + 0.1, v)) })}
                              />
                              <NumField label="X" value={selectedOverlay.x}
                                onCommit={(v) => editOverlay({ ...selectedOverlay, x: Math.round(Math.max(0, Math.min(sourceWidth - selectedOverlay.w, v))) })} />
                              <NumField label="Y" value={selectedOverlay.y}
                                onCommit={(v) => editOverlay({ ...selectedOverlay, y: Math.round(Math.max(0, Math.min(sourceHeight - selectedOverlay.h, v))) })} />
                              <NumField label="Rộng" value={selectedOverlay.w}
                                onCommit={(v) => editOverlay({ ...selectedOverlay, w: Math.round(Math.max(20, Math.min(sourceWidth - selectedOverlay.x, v))) })} />
                              <NumField label="Cao" value={selectedOverlay.h}
                                onCommit={(v) => editOverlay({ ...selectedOverlay, h: Math.round(Math.max(20, Math.min(sourceHeight - selectedOverlay.y, v))) })} />
                            </div>

                            <div className="grid grid-cols-2 gap-2 rounded-md border border-border p-2">
                              <PropLabel label={`Độ mờ: ${selectedOverlay.opacity ?? 100}%`}><input type="range" min={0} max={100} className="w-full accent-primary" value={selectedOverlay.opacity ?? 100} onChange={(e) => editOverlay({ ...selectedOverlay, opacity: Number(e.target.value) })} /></PropLabel>
                              <PropLabel label="Blend mode"><select className="h-8 w-full rounded border border-border bg-background px-2 text-xs" value={selectedOverlay.blendMode ?? 'normal'} onChange={(e) => editOverlay({ ...selectedOverlay, blendMode: e.target.value as NonNullable<TextOverlay['blendMode']> })}>{['normal', 'multiply', 'screen', 'overlay', 'darken', 'lighten'].map((mode) => <option key={mode} value={mode}>{mode}</option>)}</select></PropLabel>
                              <NumField label="Layer" value={selectedOverlay.zIndex ?? 0} onCommit={(zIndex) => editOverlay({ ...selectedOverlay, zIndex: Math.round(zIndex) })} />
                              <button type="button" className="self-end h-8 rounded border border-border text-xs hover:bg-accent" onClick={() => editOverlay({ ...selectedOverlay, keyframes: [...(selectedOverlay.keyframes ?? []), { at: playheadSec, x: selectedOverlay.x, y: selectedOverlay.y, opacity: selectedOverlay.opacity ?? 100 }] })}>+ Keyframe tại playhead</button>
                            </div>

                            <PropLabel label={`Cỡ chữ: ${selectedOverlay.fontSize}px`}>
                              <input type="range" min={12} max={160} className="w-full accent-primary"
                                value={selectedOverlay.fontSize}
                                onChange={(e) => editOverlay({ ...selectedOverlay, fontSize: Number(e.target.value) })} />
                            </PropLabel>

                            <PropLabel label="Màu text">
                              <div className="flex items-center gap-1.5">
                                <input type="color" className="h-7 w-14 rounded cursor-pointer border border-border shrink-0"
                                  value={selectedOverlay.color}
                                  onChange={(e) => editOverlay({ ...selectedOverlay, color: e.target.value })} />
                                {['#ffffff', '#ffd166', '#ef476f', '#06d6a0', '#118ab2', '#000000'].map((c) => (
                                  <button
                                    key={c}
                                    type="button"
                                    className={cn(
                                      'h-5 w-5 rounded-full border transition-transform hover:scale-110',
                                      selectedOverlay.color === c ? 'border-primary ring-1 ring-primary' : 'border-border',
                                    )}
                                    style={{ backgroundColor: c }}
                                    title={c}
                                    onClick={() => editOverlay({ ...selectedOverlay, color: c })}
                                  />
                                ))}
                              </div>
                            </PropLabel>

                            <button
                              type="button"
                              className="w-full rounded-md border border-border bg-accent hover:bg-muted px-3 py-1.5 text-xs transition-colors"
                              onClick={() => editOverlay({
                                ...selectedOverlay,
                                id: crypto.randomUUID(),
                                start: Math.min(timelineDuration - 0.1, selectedOverlay.end),
                                end: Math.min(timelineDuration, selectedOverlay.end + (selectedOverlay.end - selectedOverlay.start)),
                              }, true)}
                            >
                              Nhân bản overlay
                            </button>
                            <button
                              type="button"
                              className="w-full rounded-md border border-destructive/50 text-destructive hover:bg-destructive/10 px-3 py-1.5 text-xs transition-colors"
                              onClick={() => { onOverlayDelete(selectedOverlay.id); setSelectedOverlayId(null) }}
                            >
                              Xóa text overlay
                            </button>
                          </>
                        )}
                      </div>
                    </ScrollArea>
                  </div>
  )
}
