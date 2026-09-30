import { useMemo } from 'react'
import type { JobStatus, ProjectSettings, Segment } from '@/features/project/project.types'
import SegmentCard from './SegmentCard'
import { resolvedSpeakerProfiles, speakerRoleOptions } from './speakerProfiles'
import { localize, useLocale } from '@/app/i18n'
import { expandSegmentsForList } from './expandCompound'
import './SegmentList.css'

function safeSeg(s: Segment): Segment {
  return {
    ...s,
    source: s.source ?? '',
    translation: s.translation ?? '',
    voice: s.voice ?? 'system',
  }
}

type Props = {
  segments: Segment[]
  voices: { id: string; name: string }[]
  defaultVoice: string
  targetLang: string
  sourceLang?: string
  translator?: ProjectSettings['translator']
  projectBusy?: boolean
  videoUrl: string | null
  projectId: string | null
  logoDetection?: JobStatus['logoDetection']
  coverLogo?: boolean
  hiddenLogoTexts?: string[]
  onCoverLogoChange?: (label: string, covered: boolean) => void
  onChange: (seg: Segment) => void
  settings: ProjectSettings
  onSettings: (settings: ProjectSettings) => void
  onSegmentsReplace: (segments: Segment[]) => void | Promise<void>
}

export default function SegmentList({
  segments,
  voices,
  defaultVoice,
  targetLang,
  sourceLang,
  translator,
  projectBusy,
  videoUrl,
  projectId,
  logoDetection,
  coverLogo = false,
  hiddenLogoTexts = [],
  onCoverLogoChange,
  onChange,
  settings,
  onSettings,
  onSegmentsReplace,
}: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  // Alt+G: list vẫn hiện từng câu (không hiện shell [Compound ×N])
  const list = useMemo(
    () => expandSegmentsForList(Array.isArray(segments) ? segments : []).map(safeSeg),
    [segments],
  )
  const speakerProfiles = useMemo(() => resolvedSpeakerProfiles(list, settings, locale), [list, settings, locale])
  const roleOptions = speakerRoleOptions(locale)

  function updateSpeakerProfile(id: string, patch: Partial<(typeof speakerProfiles)[number]>) {
    const current = speakerProfiles.find((profile) => profile.id === id)
    if (!current) return
    const next = { ...current, ...patch }
    onSettings({
      ...settings,
      speakerProfiles: { ...(settings.speakerProfiles || {}), [id]: next },
      speakerVoices: { ...(settings.speakerVoices || {}), [id]: next.voice },
    })
    if (patch.voice && patch.voice !== current.voice) {
      void onSegmentsReplace(segments.map((segment) => segment.speaker === id ? {
        ...segment, voice: patch.voice!, audioUrl: undefined, audioFile: undefined, audioDuration: undefined,
      } : segment))
    }
  }

  if (list.length === 0) {
    return (
      <div className="empty">
        <p>Chưa có đoạn thoại.</p>
        <p>Tải video rồi bấm Dịch toàn bộ (nhận dạng → dịch) → Lồng tiếng.</p>
      </div>
    )
  }

  return (
    <div className="segments">
      {speakerProfiles.length > 0 && (
        <section className="speaker-manager" aria-label={t('Quản lý người nói', 'Speaker management')}>
          <div className="speaker-manager-head">
            <div><strong>{t('Người nói trong video', 'Speakers in this video')}</strong><span>{locale === 'en' ? `${speakerProfiles.length} speakers · name, color, and voice by role` : `${speakerProfiles.length} người · đổi tên, màu và giọng theo vai`}</span></div>
            <label><input type="checkbox" checked={Boolean(settings.speakerCaptionColors)} onChange={(event) => onSettings({ ...settings, speakerCaptionColors: event.target.checked })} /> {t('Màu khi xuất', 'Colors in export')}</label>
          </div>
          <div className="speaker-profile-grid">
            {speakerProfiles.map((profile) => {
              const owned = list.filter((segment) => segment.speaker === profile.id)
              return (
                <article className="speaker-profile-card" key={profile.id} style={{ borderTopColor: profile.color }}>
                  <div className="speaker-profile-name">
                    <input type="color" value={profile.color} aria-label={`${t('Màu', 'Color')} ${profile.name}`} onChange={(event) => updateSpeakerProfile(profile.id, { color: event.target.value })} />
                    <input list={`speaker-role-options-${profile.id}`} value={profile.name} aria-label={`${t('Tên vai', 'Role name')} ${profile.id}`} onChange={(event) => updateSpeakerProfile(profile.id, { name: event.target.value })} />
                    <datalist id={`speaker-role-options-${profile.id}`}>
                      {roleOptions.map((role) => <option key={role} value={role} />)}
                    </datalist>
                  </div>
                  <select value={profile.voice} aria-label={`Giọng ${profile.name}`} onChange={(event) => updateSpeakerProfile(profile.id, { voice: event.target.value })}>{voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}</select>
                  <small>{owned.length} {t('câu', 'segments')} · {owned.reduce((sum, segment) => sum + Math.max(0, segment.end - segment.start), 0).toFixed(1)}s</small>
                </article>
              )
            })}
          </div>
        </section>
      )}
      {(() => {
        const rawLabels = [
          logoDetection?.text,
          ...(logoDetection?.tracks || []).map((track) => track.text),
        ]
          .map((text) => text?.trim())
          .filter((text): text is string => Boolean(text))
        // Watermark OCR may misread one CJK glyph on a moving handle.  Display
        // the most frequently observed handle once, not several near-duplicates.
        const handles = rawLabels.filter((text) => text.startsWith('@'))
        const handle = handles.length
          ? Array.from(new Set(handles)).sort(
              (a, b) => handles.filter((text) => text === b).length - handles.filter((text) => text === a).length,
            )[0]
          : undefined
        // OCR sees the final + as 十 on some frames.  Both are the same
        // AI-generated watermark, never two separate controls.
        const generated = rawLabels.some((text) => text.includes('生成'))
        const labels = [
          ...(generated ? ['AI生成+'] : []),
          ...Array.from(
            new Set(
              rawLabels.filter((text) => !text.startsWith('@') && !text.includes('生成')),
            ),
          ),
          ...(handle ? [handle] : []),
        ]
        if (!labels.length) return null
        const isExcluded = (label: string) =>
          hiddenLogoTexts.includes(label) ||
          (label.startsWith('@') && hiddenLogoTexts.some((text) => text.startsWith('@'))) ||
          (label.includes('生成') && hiddenLogoTexts.some((text) => text.includes('生成')))
        return (
          <div className="logo-list" aria-label="Logo phát hiện trong video">
            {labels.map((label) => {
              const dynamic = label.startsWith('@')
              // Handle nền tảng chạy quanh khung hình: không thể che đúng nếu
              // chỉ dùng bbox OCR theo frame, nên mặc định luôn tắt.
              const covered = !dynamic && coverLogo && !isExcluded(label)
              return (
              <section className={`logo-summary${dynamic ? ' logo-summary--dynamic' : ''}`} key={label}>
                <div className="logo-summary__title">Logo</div>
                <div className="logo-summary__content">
                  <span className="logo-summary__caption">
                    {dynamic ? 'Watermark động' : 'Watermark cố định'}
                  </span>
                  <strong title={label}>{label}</strong>
                </div>
                <label className="logo-summary__cover">
                  <input
                    type="checkbox"
                    checked={covered}
                    disabled={dynamic}
                    onChange={(event) => onCoverLogoChange?.(label, event.target.checked)}
                    aria-label={dynamic ? `Không che logo động ${label}` : `Che logo ${label}`}
                  />
                  <span>{dynamic ? 'Không che tự động' : 'Che khi xuất'}</span>
                </label>
              </section>
              )
            })}
          </div>
        )
      })()}
      {list.map((seg) => (
        <SegmentCard
          key={seg.id}
          segment={seg}
          voices={voices}
          defaultVoice={defaultVoice}
          targetLang={targetLang}
          sourceLang={sourceLang}
          translator={translator}
          settings={settings}
          projectBusy={projectBusy}
          videoUrl={videoUrl}
          projectId={projectId}
          onChange={onChange}
          speakerProfiles={speakerProfiles}
        />
      ))}
    </div>
  )
}
