import { memo, useEffect, useRef, useState } from 'react'
import type { ProjectSettings, Segment, SpeakerProfile } from '@/features/project/project.types'
import { api } from '@/features/project/project.api'
import { localize, useLocale } from '@/app/i18n'
import { canReviewTranslatedDraft } from '@/app/appSettings'
import { IconPlay, IconRefresh } from '@/shared/components/Icons'
import { TranslationReviewField } from './TranslationReview'
import './SegmentCard.css'

type Props = {
  segment: Segment
  voices: { id: string; name: string }[]
  defaultVoice: string
  targetLang: string
  sourceLang?: string
  translator?: ProjectSettings['translator']
  settings: ProjectSettings
  projectBusy?: boolean
  videoUrl: string | null
  projectId: string | null
  onChange: (seg: Segment) => void
  speakerProfiles?: SpeakerProfile[]
}

function fmt(t: number, precise = false) {
  const m = Math.floor(t / 60)
  const sec = t % 60
  if (precise) {
    return `${m}:${sec.toFixed(1).padStart(4, '0')}`
  }
  const s = Math.floor(sec)
  return `${m}:${String(s).padStart(2, '0')}`
}

/** ponytail: one shared player so next ▶ stops the previous */
let activeMedia: HTMLMediaElement | null = null

function stopActive() {
  if (!activeMedia) return
  activeMedia.pause()
  activeMedia.removeAttribute('src')
  activeMedia.load()
  activeMedia = null
}

function SegmentCard({
  segment,
  voices,
  defaultVoice,
  targetLang,
  sourceLang = 'auto',
  translator = 'google',
  settings,
  projectBusy = false,
  videoUrl,
  projectId,
  onChange,
  speakerProfiles = [],
}: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const voice = !segment.voice || segment.voice === 'system' ? defaultVoice : segment.voice
  const sourceSafe = segment.source ?? ''
  const translationSafe = segment.translation ?? ''
  const dur = segment.audioDuration ?? Math.max(0.1, segment.end - segment.start)
  // Local draft — gõ không re-render 389 card / không PUT mỗi phím
  const [draftSource, setDraftSource] = useState(sourceSafe)
  const [draftTranslation, setDraftTranslation] = useState(translationSafe)
  const draftSourceRef = useRef(sourceSafe)
  const draftTranslationRef = useRef(translationSafe)
  const segmentRef = useRef(segment)
  const reviewButtonRef = useRef<HTMLButtonElement>(null)
  const onChangeRef = useRef(onChange)
  const textTimer = useRef<number | null>(null)
  segmentRef.current = segment
  onChangeRef.current = onChange

  useEffect(() => {
    setDraftSource(sourceSafe)
    draftSourceRef.current = sourceSafe
  }, [segment.id, sourceSafe])

  useEffect(() => {
    setDraftTranslation(translationSafe)
    draftTranslationRef.current = translationSafe
  }, [segment.id, translationSafe])

  useEffect(
    () => () => {
      if (textTimer.current != null) window.clearTimeout(textTimer.current)
      // interval 50ms + <video> detached sống mãi nếu card unmount giữa chừng
      if (stopTimer.current != null) window.clearInterval(stopTimer.current)
      if (ownMedia.current) {
        ownMedia.current.pause()
        ownMedia.current.removeAttribute('src')
        ownMedia.current.load()
        if (activeMedia === ownMedia.current) activeMedia = null
        ownMedia.current = null
      }
    },
    [],
  )

  function flushText(next?: { source?: string; translation?: string }) {
    if (textTimer.current != null) {
      window.clearTimeout(textTimer.current)
      textTimer.current = null
    }
    const src = next?.source ?? draftSourceRef.current
    const tr = next?.translation ?? draftTranslationRef.current
    const cur = segmentRef.current
    if ((cur.source ?? '') === src && (cur.translation ?? '') === tr) return
    onChangeRef.current({
      ...cur,
      source: src,
      translation: tr,
      sourceSubtitle: src,
      dubSubtitle: tr,
      // text đổi → TTS cũ lệch
      ...(tr !== (cur.translation ?? '')
        ? { audioUrl: undefined, audioFile: undefined, audioDuration: undefined, captionLayout: undefined }
        : {}),
    })
  }

  function scheduleText(patch: { source?: string; translation?: string }) {
    if (patch.source !== undefined) {
      draftSourceRef.current = patch.source
      setDraftSource(patch.source)
    }
    if (patch.translation !== undefined) {
      draftTranslationRef.current = patch.translation
      setDraftTranslation(patch.translation)
    }
    if (textTimer.current != null) window.clearTimeout(textTimer.current)
    textTimer.current = window.setTimeout(() => flushText(), 450)
  }

  const chars = draftTranslation.length || draftSource.length
  const layout = (() => {
    const lay = segment.layout
    if (lay === 'mid' || lay === 'vertical' || lay === 'label') return lay
    const b = segment.bbox
    if (b && typeof b.y === 'number' && typeof b.h === 'number') {
      const cy = b.y + b.h / 2
      if (cy > 1920 * 0.18 && cy < 1920 * 0.78) return 'mid' as const
      if (cy > 1080 * 0.18 && cy < 1080 * 0.78 && b.y + b.h < 1100) return 'mid' as const
    }
    return lay || 'horizontal'
  })()
  const isOverlay = layout === 'vertical' || layout === 'label'
  const layoutBadge =
    layout === 'vertical' ? 'Dọc' : layout === 'mid' ? 'CAP-MID' : layout === 'label' ? 'Nhãn' : 'Caption'
  const layoutTitle =
    layout === 'vertical'
      ? 'Tiêu đề dọc'
      : layout === 'mid'
        ? 'Caption giữa khung (CAP-MID) — cao/thấp tùy video, không phải phụ đề đáy cố định'
        : layout === 'label'
          ? 'Nhãn trên khung'
          : 'Phụ đề đáy (Caption)'
  const dubOn = isOverlay ? segment.dub === true : segment.dub !== false
  const [busy, setBusy] = useState(false)
  const [reBusy, setReBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const stopTimer = useRef<number | null>(null)
  const ownMedia = useRef<HTMLMediaElement | null>(null)

  function clearStopTimer() {
    if (stopTimer.current != null) {
      window.clearInterval(stopTimer.current)
      stopTimer.current = null
    }
  }

  async function playVideoClip() {
    if (!videoUrl) {
      setErr('Chưa có video')
      return
    }
    const v = document.createElement('video')
    v.src = videoUrl
    v.preload = 'auto'
    activeMedia = v
    ownMedia.current = v
    await new Promise<void>((resolve, reject) => {
      v.onloadedmetadata = () => resolve()
      v.onerror = () => reject(new Error('Không tải được video'))
    })
    v.currentTime = segment.start
    await v.play()
    stopTimer.current = window.setInterval(() => {
      // activeMedia đổi (bấm ▶ card khác) → interval này mồ côi, phải tự dọn
      if (activeMedia !== v || v.paused || v.ended || v.currentTime >= segment.end - 0.05) {
        v.pause()
        clearStopTimer()
      }
    }, 50)
  }

  async function play() {
    setErr(null)
    stopActive()
    clearStopTimer()
    flushText()

    try {
      const text = draftTranslationRef.current.trim()
      if (projectId && text) {
        setBusy(true)
        const res = await api.previewTts(projectId, segment.id, {
          text,
          voice,
          lang: targetLang && targetLang !== 'none' ? targetLang : 'vi',
        })
        const updated = {
          ...segmentRef.current,
          source: draftSourceRef.current,
          translation: draftTranslationRef.current,
          audioUrl: res.audioUrl,
          audioDuration: res.duration,
        }
        onChange(updated)
        const a = new Audio(res.audioUrl)
        activeMedia = a
        ownMedia.current = a
        await a.play()
        return
      }

      await playVideoClip()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Nghe thử thất bại')
    } finally {
      setBusy(false)
    }
  }

  async function retranslate() {
    if (!projectId || !draftSourceRef.current.trim() || targetLang === 'none') return
    setErr(null)
    setReBusy(true)
    flushText()
    try {
      const res = await api.retranslate(projectId, segment.id, {
        text: draftSourceRef.current,
        sourceLang,
        targetLang,
        translator,
      })
      draftTranslationRef.current = res.translation
      setDraftTranslation(res.translation)
      onChange({
        ...segmentRef.current,
        source: draftSourceRef.current,
        translation: res.translation,
        audioUrl: undefined,
        audioFile: undefined,
        audioDuration: undefined,
      })
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Dịch lại thất bại')
    } finally {
      setReBusy(false)
    }
  }

  return (
    <article className="seg" style={segment.speaker ? { borderLeft: `4px solid ${speakerProfiles.find((p) => p.id === segment.speaker)?.color || 'var(--primary)'}` } : undefined}>
      <div className="seg-body">
        <div className="seg-rail">
          <div className="idx-row">
            <span className="idx">{String(segment.index).padStart(2, '0')}</span>
            <span className={`seg-badge seg-badge--${layout}`} title={layoutTitle}>
              {layoutBadge}
            </span>
          </div>
          {segment.speaker && (
            <select
              className="speaker-segment-select"
              aria-label={t('Người nói của đoạn', 'Segment speaker')}
              value={segment.speaker}
              onChange={(event) => {
                const profile = speakerProfiles.find((item) => item.id === event.target.value)
                onChange({
                  ...segmentRef.current,
                  speaker: event.target.value,
                  ...(profile?.voice ? { voice: profile.voice, audioUrl: undefined, audioFile: undefined, audioDuration: undefined } : {}),
                })
              }}
            >
              {speakerProfiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
            </select>
          )}
          <span className="time">
            {(() => {
              const short = segment.end - segment.start < 1.0
              return (
                <span className="time-line" title="Đầu – cuối clip (kéo trên timeline nếu sai)">
                  {fmt(segment.start, short)} – {fmt(segment.end, short)}
                </span>
              )
            })()}
          </span>
          <div className="seg-actions">
            <button
              type="button"
              className="play"
              onClick={play}
              disabled={busy || reBusy || (isOverlay && !dubOn)}
              aria-label="Nghe đoạn"
              title={isOverlay && !dubOn ? 'Bật lồng tiếng để nghe TTS' : 'Nghe TTS bản dịch'}
            >
              {busy ? '…' : <IconPlay size={12} />}
            </button>
            <button
              type="button"
              className="retranslate"
              onClick={retranslate}
              disabled={
                reBusy || busy || !projectId || !draftSource.trim() || targetLang === 'none'
              }
              aria-label={t('Dịch lại', 'Retranslate')}
              title={t('Dịch lại từ chữ nguồn', 'Translate again from the source text')}
            >
              {reBusy ? '…' : <IconRefresh size={12} />}
            </button>
            {canReviewTranslatedDraft(settings.translator) && settings.translationReviewMode !== 'off' && (
              <button type="button" className="ai-review-shortcut" disabled={projectBusy || busy || reBusy}
                aria-label={t('AI sửa câu này, giữ nguyên ý', 'Polish this sentence with AI; preserve meaning')}
                title={t('AI sửa câu này, giữ nguyên ý', 'Polish this sentence with AI; preserve meaning')}
                onClick={() => reviewButtonRef.current?.click()}>AI</button>
            )}
          </div>
          {err && <span className="play-err">{err}</span>}
        </div>

        <label className="cell">
          <span>Ngôn ngữ gốc</span>
          <textarea
            value={draftSource}
            rows={2}
            onChange={(e) => scheduleText({ source: e.target.value })}
            onBlur={() => flushText()}
          />
        </label>

        <div className="cell">
          <TranslationReviewField projectId={projectId}
            segment={{ id: segment.id, source: draftSource, translation: draftTranslation }}
            settings={settings} disabled={projectBusy || busy || reBusy} actionButtonRef={reviewButtonRef} hideAction
            onApply={(translation) => {
              draftTranslationRef.current = translation
              setDraftTranslation(translation)
              flushText({ translation })
            }}>
            <textarea value={draftTranslation} rows={2}
              aria-label={t('Bản dịch', 'Translation')}
              onChange={(e) => scheduleText({ translation: e.target.value })}
              onBlur={() => flushText()} />
          </TranslationReviewField>
        </div>

        {isOverlay ? (
          <div className="cell voice-cell voice-toggle-cell">
            <span className="voice-head">
              Giọng đọc
              <label className="dub-check" title="Bật lồng tiếng cho tiêu đề/nhãn">
                <input
                  type="checkbox"
                  checked={dubOn}
                  onChange={(e) => {
                    flushText()
                    onChange({
                      ...segmentRef.current,
                      source: draftSourceRef.current,
                      translation: draftTranslationRef.current,
                      dub: e.target.checked,
                      ...(e.target.checked
                        ? {}
                        : { audioUrl: undefined, audioFile: undefined, audioDuration: undefined }),
                    })
                  }}
                />
                Lồng tiếng
              </label>
            </span>
            {dubOn ? (
              <select
                value={voices.some((v) => v.id === voice) ? voice : defaultVoice}
                onChange={(e) => {
                  flushText()
                  onChange({
                    ...segmentRef.current,
                    source: draftSourceRef.current,
                    translation: draftTranslationRef.current,
                    dub: true,
                    voice: e.target.value,
                  })
                }}
              >
                {voices.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name}
                  </option>
                ))}
              </select>
            ) : (
              <div className="voice-off" aria-hidden>
                Chỉ burn chữ
              </div>
            )}
            <div className="voice-meta">
              <em>{chars} {t('ký tự', 'characters')}</em>
              <em>{dur < 1 ? `${dur.toFixed(2)}s` : `${dur.toFixed(1)}s`}</em>
            </div>
          </div>
        ) : (
          <label className="cell voice-cell">
            <span>Giọng đọc</span>
            <select
              value={voices.some((v) => v.id === voice) ? voice : defaultVoice}
              onChange={(e) => {
                flushText()
                onChange({
                  ...segmentRef.current,
                  source: draftSourceRef.current,
                  translation: draftTranslationRef.current,
                  voice: e.target.value,
                })
              }}
            >
              {voices.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name}
                </option>
              ))}
            </select>
            <div className="voice-meta">
              <em>{chars} {t('ký tự', 'characters')}</em>
              <em>{dur.toFixed(1)}s</em>
            </div>
          </label>
        )}
      </div>

      <div
        className="bar"
        style={{
          ['--p' as string]: `${Math.min(100, (dur / Math.max(0.1, segment.end - segment.start)) * 50)}%`,
        }}
      />
    </article>
  )
}

export default memo(SegmentCard)
