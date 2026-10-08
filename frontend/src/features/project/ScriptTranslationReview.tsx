import { useEffect, useRef, useState } from 'react'
import { localize, localizePipelineMessage, useLocale } from '@/app/i18n'
import { IconWand } from '@/shared/components/Icons'
import { api } from './project.api'
import { expandSegmentsForList } from './expandCompound'
import { TranslationReviewSettings } from './TranslationReview'
import type { ProjectSettings, Segment } from './project.types'

export function applyScriptTranslations(segments: Segment[], translations: { id: string; translation: string }[]): Segment[] {
  const byId = new Map(translations.map(row => [row.id, row.translation]))
  function apply(rows: Segment[]): Segment[] {
    return rows.map(segment => {
      const children = segment.compoundChildren ? apply(segment.compoundChildren) : undefined
      const text = byId.get(segment.id)
      const changed = text !== undefined && text !== segment.translation
      const childChanged = children?.some((child, index) => child !== segment.compoundChildren?.[index])
      if (!changed && !childChanged) return segment
      return { ...segment, ...(children ? { compoundChildren: children } : {}),
        ...(changed ? { translation: text, dubSubtitle: text } : {}),
        audioFile: undefined, audioUrl: undefined, audioDuration: undefined, captionLayout: null }
    })
  }
  return apply(segments)
}

export function ScriptTranslationReview({ projectId, segments, settings, disabled, onSettings, onApply, onSavingChange }: {
  projectId: string | null
  segments: Segment[]
  settings: ProjectSettings
  disabled: boolean
  onSettings: (settings: ProjectSettings) => void
  onApply: (segments: Segment[]) => Promise<void>
  onSavingChange: (saving: boolean) => void
}) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [pending, setPending] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [proposal, setProposal] = useState<{ key: string; translations: { id: string; translation: string }[] } | null>(null)
  const request = useRef<AbortController | null>(null)
  const applying = useRef(false)
  const cues = expandSegmentsForList(segments)
    .filter(row => !(row as Segment & { maskOnly?: boolean }).maskOnly && row.dub !== false && row.source.trim()
      && (!['label', 'vertical'].includes(row.layout || '') || row.dub === true))
    .map(row => ({ id: row.id, source: row.source, draft: row.translation, start: row.start, end: row.end, speaker: row.speaker }))
  const key = JSON.stringify([projectId, segments, settings.targetLang, settings.translationReviewTranslator,
    settings.ollamaMode, settings.ollamaModel, settings.ollamaLocalTier])
  const current = useRef({ key, disabled })
  current.current = { key, disabled }
  useEffect(() => {
    setProposal(null)
    setError('')
    setNotice('')
    setPending(false)
    return () => { request.current?.abort(); request.current = null }
  }, [key, disabled])

  async function review() {
    if (disabled || request.current || !projectId || !cues.length) return
    const controller = new AbortController()
    request.current = controller
    setPending(true); setProposal(null); setError(''); setNotice('')
    try {
      const result = await api.reviewScript(projectId, cues, settings, controller.signal)
      if (controller.signal.aborted || current.current.key !== key || current.current.disabled) return
      const ids = new Set(result.translations.map(row => row.id))
      if (result.translations.length !== cues.length || ids.size !== cues.length
        || cues.some(row => !ids.has(row.id)) || result.translations.some(row => !row.translation?.trim())) {
        throw new Error(t('AI trả thiếu hoặc sai câu. Chưa thay đổi kịch bản.', 'AI returned missing or invalid cues. The script is unchanged.'))
      }
      setProposal({ key, translations: result.translations })
    } catch (e) {
      if (!controller.signal.aborted && current.current.key === key) setError(e instanceof Error ? localizePipelineMessage(locale, e.message) : t('AI dịch thất bại.', 'AI translation failed.'))
    } finally {
      if (request.current === controller) { request.current = null; setPending(false) }
    }
  }
  async function apply() {
    if (!proposal || proposal.key !== current.current.key || current.current.disabled || applying.current) return
    applying.current = true
    setSaving(true); onSavingChange(true); setError('')
    try {
      await onApply(applyScriptTranslations(segments, proposal.translations))
      setProposal(null)
      setNotice(t('Đã lưu bản dịch. Tạo lại lồng tiếng cho các câu đã đổi.', 'Translation saved. Regenerate dubbing for changed cues.'))
    } catch (e) {
      setError(e instanceof Error ? localizePipelineMessage(locale, e.message) : t('Không lưu được kịch bản.', 'Could not save the script.'))
    } finally { applying.current = false; setSaving(false); onSavingChange(false) }
  }
  const visible = proposal?.key === key ? proposal : null
  const proposed = new Map(visible?.translations.map(row => [row.id, row.translation]))
  const buttonClass = 'rounded-md border border-primary/35 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary disabled:opacity-50'
  return <section className="mb-3 space-y-2 rounded-md border border-border bg-card p-3" aria-label={t('AI dịch toàn kịch bản', 'AI script translation')}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div><b className="text-xs">{t('AI dịch toàn kịch bản', 'AI script translation')}</b><p className="mt-1 text-xs text-muted-foreground">{t('Đọc chung ngữ cảnh, thống nhất xưng hô và lời thoại. Giữ nguyên câu, người nói và timecode.', 'Use shared context for consistent dialogue and forms of address. Preserve cues, speakers and timecodes.')}</p></div>
      <button type="button" className={buttonClass} disabled={disabled || pending || saving || !projectId || !cues.length || ['none', 'off', 'source', ''].includes(settings.targetLang)} onClick={() => void review()}><IconWand size={12} /> {pending ? t('AI đang dịch…', 'AI translating…') : t('AI dịch lại toàn bộ', 'AI retranslate all')}</button>
      {pending && <button type="button" className="text-xs text-destructive" onClick={() => { request.current?.abort(); request.current = null; setPending(false) }}>{t('Hủy', 'Cancel')}</button>}
    </div>
    <details className="text-xs"><summary className="cursor-pointer text-muted-foreground">{t('Công cụ và model AI', 'AI provider and model')} · {settings.translationReviewTranslator}</summary><div className="mt-2 max-w-xl"><TranslationReviewSettings settings={settings} onChange={onSettings} disabled={disabled || pending || saving} wholeScript /></div></details>
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    {notice && <p role="status" className="text-xs text-muted-foreground">{notice}</p>}
    {visible && <div className="space-y-2 border-t border-border pt-2">
      <div className="grid grid-cols-[2rem_1fr_1fr] gap-2 text-xs font-medium"><span>#</span><span>{t('Hiện tại', 'Current')}</span><span>{t('Đề xuất AI', 'AI suggestion')}</span></div>
      <div className="max-h-64 space-y-2 overflow-auto">{cues.map((cue, index) => <div key={cue.id} className="grid grid-cols-[2rem_1fr_1fr] gap-2 text-xs"><span>{index + 1}</span><p className="whitespace-pre-wrap text-muted-foreground">{cue.draft || cue.source}</p><p className="whitespace-pre-wrap">{proposed.get(cue.id)}</p></div>)}</div>
      <div className="flex gap-3"><button type="button" className={buttonClass} disabled={disabled || saving} onClick={() => void apply()}>{saving ? t('Đang lưu…', 'Saving…') : t('Áp dụng toàn bộ', 'Apply all')}</button><button type="button" className="text-xs text-muted-foreground" disabled={saving} onClick={() => setProposal(null)}>{t('Bỏ qua', 'Dismiss')}</button></div>
    </div>}
  </section>
}
