import React from 'react'
import type { ProjectSettings, Segment } from '@/features/project/project.types'
import { api } from '@/features/project/project.api'
import { resolvedSpeakerProfiles, speakerRoleOptions } from '@/features/project/speakerProfiles'
import { localize, useLocale } from '@/app/i18n'
import { normalizeTranslatorForEngine, translatorOptions } from '@/app/appSettings'
import { TranslationReviewSettings } from '@/features/project/TranslationReview'
import { ScrollArea } from '@/shared/ui/scroll-area'
import ProgressPopup from '@/shared/components/ProgressPopup'
import { formatTimecode, PropLabel } from '@/features/editor/lib'

type Props = {
  projectId?: string
  tab: 'workflow' | 'speakers'
  segments: Segment[]
  settings: ProjectSettings
  voices: { id: string; name: string }[]
  busy: boolean
  jobStep: string
  jobProgress: number
  onSettings: (settings: ProjectSettings) => void
  onRunPipeline?: (previewSec: number, settingsOverride?: ProjectSettings) => void | Promise<void>
  onCancel?: () => void
  onDub?: () => void
  onExport: () => void
  applyCaptionModeAll?: (mode: 'cover' | 'below' | 'above' | 'none') => void
  onUpdateSpeakerProfile: (id: string, patch: { name?: string; color?: string; voice?: string }) => void
}

export function EditorProjectPanel({ projectId, tab, segments, settings, voices, busy, jobStep, jobProgress, onSettings, onRunPipeline, onCancel, onDub, onExport, applyCaptionModeAll, onUpdateSpeakerProfile }: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [previewSec, setPreviewSec] = React.useState(() => Math.max(5, Number(settings.previewSec) || 30))
  React.useEffect(() => setPreviewSec(Math.max(5, Number(settings.previewSec) || 30)), [settings.previewSec])
  const profiles = React.useMemo(() => resolvedSpeakerProfiles(segments, settings, locale), [segments, settings, locale])
  const [resources, setResources] = React.useState<import('@/features/project/project.types').AiResource[]>([])
  const [resourceBusy, setResourceBusy] = React.useState<string | null>(null)
  const [ocrJob, setOcrJob] = React.useState<{ running: boolean; polling: boolean; progress: number; message: string; error?: string }>({ running: false, polling: false, progress: 0, message: '', error: undefined })
  const [ocrMinimized, setOcrMinimized] = React.useState(false)
  const ocrRequestLock = React.useRef(false)
  React.useEffect(() => { void api.resources().then((result) => setResources(result.items)).catch(() => setResources([])) }, [])
  React.useEffect(() => {
    if (!ocrJob.running || !ocrJob.polling || !projectId) return
    const poll = () => void api.status(projectId).then((status) => {
      setOcrJob({ running: Boolean(status.running), polling: Boolean(status.running), progress: Number(status.progress || 0), message: status.message || '', error: status.error })
    }).catch((error) => setOcrJob((job) => ({ ...job, running: false, polling: false, error: error instanceof Error ? error.message : 'OCR failed' })))
    poll()
    const id = window.setInterval(poll, 600)
    return () => window.clearInterval(id)
  }, [ocrJob.running, ocrJob.polling, projectId])
  // The left panel is unmounted when users change tabs. Re-attach its local
  // progress UI when returning to an OCR job that is already running.
  React.useEffect(() => {
    if (!projectId || ocrJob.running) return
    void api.status(projectId).then((status) => {
      if (status.running && /ocr/i.test(String(status.message || ''))) {
        setOcrMinimized(true)
        setOcrJob({ running: true, polling: true, progress: Number(status.progress || 1), message: status.message || t('OCR Translator đang chạy…', 'OCR Translator is running…'), error: status.error })
      }
    }).catch(() => { /* status is optional on panel mount */ })
  }, [projectId])
  const selectRecognitionEngine = (engine: ProjectSettings['engine']) => {
    onSettings({ ...settings, engine, translator: normalizeTranslatorForEngine(engine, settings.translator) })
  }

  return <><ScrollArea className="h-full scrollbar-hidden"><div className="space-y-3 p-3">
    <div className="border-b border-border pb-1 text-sm text-muted-foreground">{tab === 'workflow' ? t('Quy trình dự án', 'Project workflow') : t('Người nói', 'Speakers')}</div>
    {tab === 'workflow' ? <>
      <p className="text-[11px] leading-snug text-muted-foreground">{t('Thiết lập và chạy toàn bộ pipeline cho dự án này.', 'Configure and run this project’s complete pipeline.')}</p>
      <div className="grid grid-cols-2 gap-2">
        <PropLabel label={t('Nhận dạng', 'Recognition')}><select className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs" value={settings.engine} disabled={busy} onChange={(e) => selectRecognitionEngine(e.target.value as ProjectSettings['engine'])}><option value="whisper">Whisper · {t('chất lượng', 'quality')}</option><option value="capcut">{t('CapCut cloud', 'CapCut cloud')}</option><option value="paddleocr">OCR</option><option value="subtitle">SRT</option></select></PropLabel>
        <PropLabel label={t('Công cụ dịch', 'Translator')}><select className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs" value={settings.translator} disabled={busy} onChange={(e) => onSettings({ ...settings, translator: e.target.value as ProjectSettings['translator'] })}>{translatorOptions(settings.engine).map(({ id, label }) => <option key={id} value={id}>{label}</option>)}</select></PropLabel>
        <PropLabel label={t('Ngôn ngữ gốc', 'Source language')}><select className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs" value={settings.sourceLang} disabled={busy} onChange={(e) => onSettings({ ...settings, sourceLang: e.target.value })}><option value="auto">{t('Tự động', 'Auto')}</option><option value="zh">中文</option><option value="en">English</option><option value="ja">日本語</option><option value="ko">한국어</option><option value="vi">Tiếng Việt</option></select></PropLabel>
        <PropLabel label={t('Dịch sang', 'Translate to')}><select className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs" value={settings.targetLang} disabled={busy} onChange={(e) => onSettings({ ...settings, targetLang: e.target.value })}><option value="vi">Tiếng Việt</option><option value="en">English</option><option value="zh">中文</option><option value="ja">日本語</option><option value="ko">한국어</option><option value="none">{t('Không dịch', 'No translation')}</option></select></PropLabel>
        <PropLabel label={t('Phụ đề', 'Subtitles')}>
          <select
            className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs"
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
              const v = e.target.value as 'cover' | 'below' | 'above' | 'none'
              if (applyCaptionModeAll) {
                applyCaptionModeAll(v)
                return
              }
              if (v === 'cover') {
                onSettings({ ...settings, coverHardsubs: true, burnSubs: true })
              } else if (v === 'below') {
                onSettings({ ...settings, coverHardsubs: false, burnSubs: true, captionPlacement: 'below' })
              } else if (v === 'above') {
                onSettings({ ...settings, coverHardsubs: false, burnSubs: true, captionPlacement: 'above' })
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
        </PropLabel>
        <PropLabel label={t('Cỡ chữ', 'Font size')}>
          <select
            className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs"
            value={String(settings.subtitleFontSize ?? 0)}
            disabled={busy || !settings.burnSubs}
            onChange={(e) => onSettings({ ...settings, subtitleFontSize: Number(e.target.value) })}
          >
            <option value="0">{t('Tự động', 'Auto')}</option>
            {[16, 18, 20, 22, 24, 28, 32, 36, 40, 48, 56, 64].map((px) => (
              <option key={px} value={px}>{px} px</option>
            ))}
          </select>
        </PropLabel>
      </div>
      <TranslationReviewSettings settings={settings} onChange={onSettings} disabled={busy} />
      <label className="flex cursor-pointer items-center justify-between gap-2 rounded-md border border-border px-2 py-2 text-xs"><span><b className="block text-foreground">{t('Tách người nói', 'Separate speakers')}</b><span className="text-[10px] text-muted-foreground">{t('Phân vai và dùng giọng riêng.', 'Assign roles and individual voices.')}</span></span><input type="checkbox" className="size-4 accent-primary" checked={Boolean(settings.speakerDiarization)} disabled={busy || settings.engine !== 'whisper'} onChange={(e) => onSettings({ ...settings, speakerDiarization: e.target.checked })} /></label>
      {settings.speakerDiarization && settings.engine === 'whisper' && (
        <div className="flex items-center justify-between gap-2 rounded-md border border-border px-2 py-1.5 text-xs">
          <span>{t('Số người nói', 'Speaker count')}</span>
          <select
            value={settings.speakerCount || 0}
            disabled={busy}
            className="h-7 rounded border border-border bg-background px-2 text-xs"
            onChange={(e) => onSettings({ ...settings, speakerCount: Number(e.target.value) })}
          >
            <option value={0}>{t('Tự phát hiện', 'Auto-detect')}</option>
            {[2, 3, 4, 5, 6, 7, 8].map((count) => (
              <option key={count} value={count}>{locale === 'en' ? `${count} speakers` : `${count} người`}</option>
            ))}
          </select>
        </div>
      )}
      <button type="button" disabled={busy || ocrJob.running || !projectId} className="w-full rounded-md border border-violet-400/50 bg-violet-500/10 px-2 py-2 text-xs font-medium text-violet-700 hover:bg-violet-500/20 disabled:opacity-50" onClick={() => { if (!projectId || ocrRequestLock.current) return; ocrRequestLock.current = true; setOcrMinimized(false); setOcrJob({ running: true, polling: false, progress: 1, message: t('Đang khởi tạo OCR Translator…', 'Starting OCR Translator…') }); void api.runOcrTranslate(projectId, settings).then(() => setOcrJob((job) => ({ ...job, polling: true }))).catch(async (error) => { const detail = error instanceof Error ? error.message : 'OCR failed'; if (/OCR Translator.*đang chạy|OCR Translator.*running/i.test(detail)) { try { const status = await api.status(projectId); setOcrJob({ running: Boolean(status.running), polling: Boolean(status.running), progress: Number(status.progress || 1), message: status.message || t('OCR Translator đang chạy…', 'OCR Translator is running…'), error: status.running ? undefined : detail }); return } catch { /* show original error below */ } } setOcrMinimized(false); setOcrJob({ running: false, polling: false, progress: 0, message: '', error: detail }) }).finally(() => { window.setTimeout(() => { ocrRequestLock.current = false }, 500) }) }}>{t('OCR Translator → track riêng', 'OCR Translator → separate track')}</button>
      <div className="rounded-md border border-border p-2"><div className="mb-1 text-xs font-medium">{t('Track phụ đề xuất', 'Export subtitle track')}</div><select className="h-8 w-full rounded border border-border bg-background px-2 text-xs" value={settings.subtitleExportTrack ?? 'dub'} disabled={busy} onChange={(e) => onSettings({ ...settings, subtitleExportTrack: e.target.value as 'source' | 'dub' | 'both' })}><option value="dub">{t('Phụ đề lồng tiếng', 'Dub subtitle')}</option><option value="source">{t('Phụ đề gốc', 'Source subtitle')}</option><option value="both">{t('Cả hai track', 'Both tracks')}</option></select></div>
      <div className="rounded-md border border-border p-2"><div className="flex items-center justify-between gap-2"><b className="text-xs">{t('Preview', 'Preview')}</b><input type="number" min={5} max={3600} value={previewSec} disabled={busy} className="h-7 w-16 rounded border border-border bg-background px-1.5 text-right text-xs" aria-label={t('Số giây preview', 'Preview seconds')} onChange={(e) => setPreviewSec(Math.max(5, Math.min(3600, Number(e.target.value) || 5)))} /></div><div className="mt-2 grid grid-cols-2 gap-1.5"><button type="button" disabled={busy || !onRunPipeline} className="rounded-md border border-border px-2 py-2 text-xs font-medium hover:bg-accent disabled:opacity-50" onClick={() => { const next = { ...settings, previewSec }; onSettings(next); void onRunPipeline?.(previewSec, next) }}>{t('Chạy preview', 'Run preview')}</button><button type="button" disabled={busy || !onRunPipeline} className="rounded-md bg-primary px-2 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50" onClick={() => void onRunPipeline?.(0, settings)}>{t('Chạy toàn video', 'Run full video')}</button></div>{busy && <div className="mt-2 flex justify-between gap-2 text-[11px] text-muted-foreground"><span className="truncate">{jobStep || t('Đang xử lý', 'Processing')} · {Math.round(jobProgress || 0)}%</span>{onCancel && <button type="button" className="font-medium text-destructive hover:underline" onClick={onCancel}>{t('Hủy', 'Cancel')}</button>}</div>}</div>
      <div className="grid grid-cols-2 gap-1.5"><button type="button" disabled={busy || !segments.length || !onDub} className="rounded-md border border-primary/40 bg-primary/10 px-2 py-2 text-xs font-medium text-primary hover:bg-primary/20 disabled:opacity-50" onClick={() => void onDub?.()}>{settings.speakerDiarization ? t('Tạo TTS theo vai', 'Generate TTS by role') : t('Tạo TTS', 'Generate TTS')}</button><button type="button" disabled={busy} className="rounded-md border border-border px-2 py-2 text-xs font-medium hover:bg-accent disabled:opacity-50" onClick={onExport}>{t('Xuất video', 'Export video')}</button></div>
      <details className="rounded-md border border-border p-2"><summary className="cursor-pointer text-xs font-medium">{t('Màu & LUT', 'Color & LUT')}</summary><div className="mt-2 space-y-2">{([['brightness', t('Sáng', 'Brightness'), -100, 100], ['contrast', t('Tương phản', 'Contrast'), -100, 100], ['saturation', t('Bão hòa', 'Saturation'), 0, 200], ['temperature', t('Nhiệt độ', 'Temperature'), -100, 100], ['tint', 'Tint', -100, 100]] as const).map(([key, label, min, max]) => { const color = settings.colorAdjust ?? { brightness: 0, contrast: 0, saturation: 100, temperature: 0, tint: 0 }; return <label key={key} className="grid grid-cols-[72px_1fr_30px] items-center gap-1 text-[10px]"><span>{label}</span><input type="range" min={min} max={max} value={color[key]} disabled={busy} className="accent-primary" onChange={(e) => onSettings({ ...settings, colorAdjust: { ...color, [key]: Number(e.target.value) } })} /><span className="text-right text-muted-foreground">{color[key]}</span></label> })}<p className="text-[10px] text-muted-foreground">{t('Preview và export dùng cùng điều chỉnh. LUT .cube chọn từ Media sẽ được thêm ở bước xuất.', 'Preview and export share adjustments. A .cube LUT from Media is applied during export.')}</p></div></details>
      <details className="rounded-md border border-border bg-accent/20 p-2" open={resources.some((item) => !item.installed)}><summary className="cursor-pointer text-xs font-medium">{t('Tài nguyên AI', 'AI resources')}</summary><div className="mt-2 space-y-1.5">{resources.map((resource) => <div key={resource.id} className="flex items-center justify-between gap-2 text-[11px]"><span className="min-w-0 truncate"><b className={resource.installed ? 'text-emerald-600' : 'text-amber-600'}>{resource.installed ? '●' : '○'}</b> {resource.id === 'diarization' ? t('Sherpa-ONNX (Tách người nói)', 'Sherpa-ONNX (Speaker diarization)') : resource.name}<small className="ml-1 text-muted-foreground">{resource.provider}</small></span>{resource.installed ? <span className="text-muted-foreground">{t('Sẵn sàng', 'Ready')}</span> : <button type="button" disabled={resourceBusy !== null} className="rounded border border-primary/40 px-1.5 py-1 text-primary hover:bg-primary/10 disabled:opacity-50" onClick={() => { setResourceBusy(resource.id); void api.installResource(resource.id).catch((e) => alert(e instanceof Error ? e.message : 'Lỗi cài đặt')).finally(() => { void api.resources().then((r) => setResources(r.items)); setResourceBusy(null) }) }}>{resourceBusy === resource.id ? t('Đang tải…', 'Loading…') : t('Tải', 'Install')}</button>}</div>)}</div></details>
    </> : profiles.length === 0 ? <p className="py-4 text-center text-[11px] leading-snug text-muted-foreground">{t('Bật Tách người nói trong Quy trình rồi chạy nhận dạng để có dữ liệu vai.', 'Enable speaker separation in Workflow, then run recognition to get roles.')}</p> : <>
      <label className="flex items-center justify-between rounded-md border border-border bg-accent/30 px-2 py-2 text-[11px]"><span><b className="block text-foreground">{t('Màu phụ đề theo vai', 'Caption color by role')}</b><span className="text-muted-foreground">{t('Preview và video xuất', 'Preview and exported video')}</span></span><input type="checkbox" className="size-4 accent-primary" checked={Boolean(settings.speakerCaptionColors)} disabled={busy} onChange={(e) => onSettings({ ...settings, speakerCaptionColors: e.target.checked })} /></label>
      {profiles.map((profile) => { const owned = segments.filter((segment) => segment.speaker === profile.id); const seconds = owned.reduce((sum, segment) => sum + Math.max(0, segment.end - segment.start), 0); return <div key={profile.id} className="space-y-1.5 rounded-md border border-border bg-background p-2" style={{ borderLeft: `4px solid ${profile.color}` }}><div className="flex gap-1.5"><input type="color" className="size-8 shrink-0 rounded border border-border bg-transparent p-0.5" value={profile.color} disabled={busy} aria-label={`${t('Màu', 'Color')} ${profile.name}`} onChange={(e) => onUpdateSpeakerProfile(profile.id, { color: e.target.value })} /><input className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 text-xs font-medium" value={profile.name} list={`speaker-role-${profile.id}`} disabled={busy} aria-label={`${t('Tên', 'Name')} ${profile.id}`} onChange={(e) => onUpdateSpeakerProfile(profile.id, { name: e.target.value })} /><datalist id={`speaker-role-${profile.id}`}>{speakerRoleOptions(locale).map((role) => <option key={role} value={role} />)}</datalist></div><select className="h-8 w-full rounded-md border border-border bg-background px-2 text-[11px]" value={profile.voice || ''} disabled={busy} onChange={(e) => onUpdateSpeakerProfile(profile.id, { voice: e.target.value })}><option value="">{t('Giọng mặc định', 'Default voice')}</option>{voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}</select><div className="flex justify-between text-[10px] text-muted-foreground"><span>{owned.length} {t('đoạn', 'segments')}</span><span>{formatTimecode(seconds)}</span></div></div> })}
    </>}
  </div></ScrollArea>
  <ProgressPopup active={ocrJob.running || Boolean(ocrJob.error)} minimized={ocrMinimized && ocrJob.running} title={t('OCR Translator', 'OCR Translator')} message={ocrJob.message} progress={ocrJob.progress} error={ocrJob.error} running={ocrJob.running} onMinimize={() => setOcrMinimized(true)} onRestore={() => setOcrMinimized(false)} onCancel={projectId ? () => { void api.cancel(projectId); setOcrMinimized(false); setOcrJob((job) => ({ ...job, running: false, polling: false })) } : undefined} /></>
}
