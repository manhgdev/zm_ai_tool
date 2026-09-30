import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { localize, localizePipelineMessage, useLocale } from '@/app/i18n'
import { canReviewTranslatedDraft, translationReviewOptions } from '@/app/appSettings'
import { IconWand } from '@/shared/components/Icons'
import { api } from './project.api'
import type { ProjectSettings, Segment } from './project.types'

export function TranslationReviewSettings({ settings, onChange, disabled = false }: {
  settings: ProjectSettings
  onChange: (settings: ProjectSettings) => void
  disabled?: boolean
}) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [providers, setProviders] = useState<string[]>([])
  const [models, setModels] = useState<{ id: string; label: string; free: boolean }[]>([])
  const [model, setModel] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    const refresh = () => void api.getConfig().then((cfg) => {
      if (active) setProviders(Object.entries(cfg.cloud || {}).filter(([, value]) => value.apiKeySet).map(([id]) => id))
    }).catch(() => { if (active) setProviders([]) })
    refresh()
    window.addEventListener('focus', refresh)
    return () => { active = false; window.removeEventListener('focus', refresh) }
  }, [])
  const provider = providers.includes(settings.translationReviewTranslator) ? settings.translationReviewTranslator : ''
  useEffect(() => {
    if (!provider || settings.translationReviewMode === 'off') { setModels([]); return }
    let active = true
    setLoading(true)
    setError('')
    setModels([])
    setModel('')
    void Promise.all([
      api.getConfig(),
      fetch(`/api/chat/models?provider=${encodeURIComponent(provider)}&includePaid=true`).then((response) => {
        if (!response.ok) throw new Error('Model discovery failed')
        return response.json() as Promise<{ models?: { id: string; label?: string; free?: boolean; capabilities?: string[] }[] }>
      }),
    ]).then(([cfg, data]) => {
      if (!active) return
      setModel(cfg.cloud?.[provider as keyof typeof cfg.cloud]?.model || '')
      setModels((data.models || []).filter((item) => item.id && (!item.capabilities?.length || item.capabilities.includes('text')) && !/embedding|moderation|whisper|audio|speech|tts|guard|riva-translate/i.test(item.id)).map((item) => ({ id: item.id, label: item.label || item.id, free: item.free === true })))
    }).catch(() => { if (active) setModels([]) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [provider, settings.translationReviewMode])
  async function selectModel(value: string) {
    if (!provider || saving) return
    setSaving(true)
    setError('')
    try {
      await api.saveConfig({ cloud: { [provider]: { model: value } } })
      setModel(value)
    } catch {
      setError(t('Không lưu được model AI. Thử lại.', 'Could not save the AI model. Retry.'))
    } finally { setSaving(false) }
  }
  if (!canReviewTranslatedDraft(settings.translator) || !providers.length) return null
  const selectClass = 'h-8 min-w-0 w-full rounded-md border border-border bg-background px-2 text-xs text-foreground disabled:opacity-50'
  return (
    <div className="grid grid-cols-2 gap-2">
      <label className="flex min-w-0 flex-col gap-1 text-xs text-muted-foreground">
        <span>{t('AI chỉnh bản dịch', 'AI translation review')}</span>
        <select className={selectClass} value={settings.translationReviewMode} disabled={disabled}
          title={t('Tự động chỉnh sau khi dịch; thủ công xem đề xuất trước khi áp dụng.', 'Automatic reviews after translation; manual lets you review suggestions before applying.')}
          onChange={(e) => onChange({ ...settings, translationReviewMode: e.target.value as ProjectSettings['translationReviewMode'] })}>
          <option value="off">{t('Tắt', 'Off')}</option>
          <option value="manual">{t('Thủ công', 'Manual')}</option>
          <option value="auto">{t('Tự động sau khi dịch', 'Automatic after translation')}</option>
        </select>
      </label>
      {settings.translationReviewMode !== 'off' && (
        <label className="flex min-w-0 flex-col gap-1 text-xs text-muted-foreground">
          <span>{t('Công cụ AI', 'AI provider')}</span>
          <select className={selectClass} value={provider} disabled={disabled || saving}
            aria-label={t('Nguồn AI dùng để sửa bản dịch', 'AI source used for translation review')}
            title={settings.translationReviewTranslator === 'ollama'
              ? `${settings.ollamaMode} · ${settings.ollamaModel}`
              : t('Dùng API key và model trong Cấu hình → API dịch.', 'Uses the API key and model in Settings → Translation APIs.')}
            onChange={(e) => onChange({ ...settings, translationReviewTranslator: e.target.value as ProjectSettings['translationReviewTranslator'] })}>
            {!provider && <option value="" disabled>{t('Chọn công cụ AI', 'Select AI provider')}</option>}
            {translationReviewOptions().filter(({ id }) => providers.includes(id)).map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
          </select>
        </label>
      )}
      {settings.translationReviewMode !== 'off' && provider && (
        <label className="col-span-2 flex min-w-0 flex-col gap-1 text-xs text-muted-foreground">
          <span>{t('Model AI', 'AI model')}</span>
          <select className={selectClass} value={model} disabled={disabled || loading || saving || !models.length} onChange={(event) => void selectModel(event.target.value)}>
            {!models.length && <option value="">{loading ? t('Đang tải model…', 'Loading models…') : t('Chưa tải được model', 'No model list loaded')}</option>}
            {model && !models.some((item) => item.id === model) && <option value={model}>{model}</option>}
            {models.some((item) => item.free) && <optgroup label={t('Model free dùng được', 'Free models available')}>
              {models.filter((item) => item.free).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </optgroup>}
            {models.some((item) => !item.free) && <optgroup label={t('Model cần credit', 'Models requiring credit')}>
              {models.filter((item) => !item.free).map((item) => <option key={item.id} value={item.id}>{item.label} — {t('cần credit', 'credit required')}</option>)}
            </optgroup>}
          </select>
        </label>
      )}
      {error && <p role="alert" className="col-span-2 text-xs text-destructive">{error}</p>}
    </div>
  )
}

/** Same proposal and stale-response guards in Clone Video and the editor. */
export function TranslationReviewField({ projectId, segment, settings, disabled = false, onApply, children, actionButtonRef, hideAction = false }: {
  projectId: string | null
  segment: Pick<Segment, 'id' | 'source' | 'translation'>
  settings: ProjectSettings
  disabled?: boolean
  onApply: (translation: string) => void | Promise<void>
  children: ReactNode
  actionButtonRef?: RefObject<HTMLButtonElement | null>
  hideAction?: boolean
}) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const reviewEnabled = canReviewTranslatedDraft(settings.translator) && settings.translationReviewMode !== 'off'
  const [pending, setPending] = useState(false)
  const [proposal, setProposal] = useState<{ key: string; translation: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const request = useRef<AbortController | null>(null)
  const key = JSON.stringify([projectId, segment.id, segment.source, segment.translation,
    settings.targetLang, settings.translationReviewMode, settings.translationReviewTranslator,
    settings.ollamaMode, settings.ollamaModel, settings.ollamaLocalTier])
  const current = useRef({ key, disabled })
  current.current = { key, disabled }
  useEffect(() => {
    setProposal(null)
    setError(null)
    setPending(false)
    return () => { request.current?.abort(); request.current = null }
  }, [key])

  async function review() {
    if (disabled || request.current || !projectId) return
    const controller = new AbortController()
    request.current = controller
    setPending(true)
    setProposal(null)
    setError(null)
    try {
      const result = await api.reviewTranslation(projectId, segment.id, {
        text: segment.source,
        translation: segment.translation,
        targetLang: settings.targetLang,
        translator: settings.translationReviewTranslator,
        ollamaMode: settings.ollamaMode,
        ollamaModel: settings.ollamaModel,
        ollamaLocalTier: settings.ollamaLocalTier,
      }, controller.signal)
      if (!controller.signal.aborted && current.current.key === key) {
        setProposal({ key, translation: result.translation })
      }
    } catch (e) {
      if (!controller.signal.aborted && current.current.key === key) {
        setError(e instanceof Error ? localizePipelineMessage(locale, e.message) : t('AI chỉnh bản dịch thất bại', 'AI review failed'))
      }
    } finally {
      if (request.current === controller) { request.current = null; setPending(false) }
    }
  }

  const visibleProposal = proposal?.key === key ? proposal : null
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">{t('Bản dịch', 'Translation')}</span>
        {reviewEnabled && !hideAction && (
          <button type="button" className="translation-review-action flex shrink-0 items-center gap-1 rounded-md border border-primary/35 bg-primary/10 px-2 py-1 text-xs font-medium text-primary hover:bg-primary/15 hover:text-primary disabled:opacity-50"
            disabled={disabled || pending || !projectId || !segment.source.trim() || !segment.translation.trim() || ['none', 'off', 'source', ''].includes(settings.targetLang)}
            aria-label={t('AI chỉnh bản dịch', 'Review translation with AI')}
            title={t('AI sửa câu này, giữ nguyên ý', 'Polish this translation with AI while preserving meaning')}
            onClick={() => void review()}>
            <IconWand size={12} /> {pending ? t('Đang sửa…', 'Reviewing…') : t('AI sửa câu này, giữ nguyên ý', 'Polish this sentence with AI; preserve meaning')}
          </button>
        )}
        {reviewEnabled && hideAction && <button ref={actionButtonRef} type="button" className="sr-only" tabIndex={-1} aria-hidden="true" onClick={() => void review()} />}
      </div>
      {children}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {visibleProposal && (
        <div className="border-t border-border pt-1.5 text-xs" aria-label={t('Đề xuất từ AI', 'AI suggestion')}>
          <p className="whitespace-pre-wrap break-words text-foreground">{visibleProposal.translation === segment.translation
            ? t('AI giữ nguyên bản dịch.', 'AI kept the translation.') : visibleProposal.translation}</p>
          <div className="mt-1 flex gap-3">
            {visibleProposal.translation !== segment.translation && (
              <button type="button" className="text-primary disabled:opacity-50" disabled={disabled} onClick={() => {
                if (current.current.disabled || current.current.key !== visibleProposal.key) return
                void onApply(visibleProposal.translation)
                setProposal(null)
              }}>{t('Áp dụng', 'Apply')}</button>
            )}
            <button type="button" className="text-muted-foreground" onClick={() => setProposal(null)}>{t('Bỏ qua', 'Dismiss')}</button>
          </div>
        </div>
      )}
    </div>
  )
}
