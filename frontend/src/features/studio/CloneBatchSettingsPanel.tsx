import { useEffect, useState } from 'react'
import { localize, useLocale } from '@/app/i18n'
import { applyEngineProfile, normalizeTranslatorForEngine, snapshotEngineProfile, translatorOptions } from '@/app/appSettings'
import type { ProjectSettings } from '@/features/project/project.types'

type Voice = { id: string; name: string }
type Props = {
  settings: ProjectSettings
  voices: Voice[]
  onChange: (settings: ProjectSettings) => void
}

const LANGUAGES = [
  ['vi', 'Tiếng Việt', 'Vietnamese'],
  ['en', 'Tiếng Anh', 'English'],
  ['zh', 'Tiếng Trung', 'Chinese'],
  ['ja', 'Tiếng Nhật', 'Japanese'],
  ['ko', 'Tiếng Hàn', 'Korean'],
] as const

export function CloneBatchSettingsPanel({ settings, voices, onChange }: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [ollamaLocalModels, setOllamaLocalModels] = useState<string[]>([])
  const set = <K extends keyof ProjectSettings>(key: K, value: ProjectSettings[K]) =>
    onChange({ ...settings, [key]: value })
  const captionMode = !settings.burnSubs || settings.targetLang === 'none'
    ? 'none'
    : settings.coverHardsubs
      ? 'cover'
      : settings.captionPlacement
  const fontSizes = [24, 32, 40, 48, 56, 64, 72, 80]
  const fontSizeOptions = settings.subtitleFontSize === 0 || fontSizes.includes(settings.subtitleFontSize)
    ? fontSizes
    : [...fontSizes, settings.subtitleFontSize].sort((a, b) => a - b)

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

  function selectEngine(engine: ProjectSettings['engine']) {
    const next = applyEngineProfile(snapshotEngineProfile(settings), engine)
    onChange({ ...next, translator: normalizeTranslatorForEngine(engine, next.translator) })
  }

  function setCaptionMode(mode: string) {
    if (mode === 'cover') {
      onChange({ ...settings, coverHardsubs: true, burnSubs: true })
    } else if (mode === 'above' || mode === 'below') {
      onChange({
        ...settings,
        coverHardsubs: false,
        burnSubs: true,
        captionPlacement: mode,
      })
    } else {
      onChange({ ...settings, burnSubs: false })
    }
  }

  return (
    <section className="studio-card clone-batch-settings">
      <div className="clone-settings-head">
        <div>
          <h2>{t('Cấu hình Clone hàng loạt', 'Batch Clone setup')}</h2>
          <p className="muted">{t(
            'Cùng cấu hình pipeline với Clone Video, áp dụng cho mọi nguồn trong lần thêm hàng đợi này.',
            'Uses the same pipeline settings as Clone Video for every source added this time.',
          )}</p>
        </div>
      </div>

      <div className="clone-settings-grid">
        <label>
          <span>{t('Nhận dạng', 'Recognition')}</span>
          <select value={settings.engine} onChange={(e) => selectEngine(e.target.value as ProjectSettings['engine'])}>
            <option value="whisper">{t('Giọng nói (Whisper)', 'Speech (Whisper)')}</option>
            <option value="capcut">{t('Giọng nói (CapCut cloud)', 'Speech (CapCut cloud)')}</option>
            <option value="paddleocr">{t('Chữ trên màn (OCR)', 'On-screen text (OCR)')}</option>
          </select>
          <small>{t('Chưa hỗ trợ SRT riêng.', 'Per-video SRT is unavailable.')}</small>
        </label>

        <label>
          <span>{t('Công cụ dịch', 'Translator')}</span>
          <select
            value={settings.translator}
            onChange={(e) => set('translator', e.target.value as ProjectSettings['translator'])}
          >
            {translatorOptions(settings.engine).map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
          </select>
        </label>

        <label>
          <span>{t('Ngôn ngữ gốc', 'Original language')}</span>
          <select value={settings.sourceLang} onChange={(e) => set('sourceLang', e.target.value)}>
            <option value="auto">{t('Tự động nhận diện', 'Auto detect')}</option>
            {LANGUAGES.map(([id, vi, en]) => <option key={id} value={id}>{t(vi, en)}</option>)}
          </select>
        </label>

        <label>
          <span>{t('Ngôn ngữ dịch', 'Translation language')}</span>
          <select value={settings.targetLang} onChange={(e) => set('targetLang', e.target.value)}>
            <option value="none">{t('Không dịch (giữ chữ nguồn)', 'Do not translate')}</option>
            {LANGUAGES.map(([id, vi, en]) => <option key={id} value={id}>{t(vi, en)}</option>)}
          </select>
        </label>

        <label>
          <span>{t('Khớp thời lượng', 'Duration matching')}</span>
          <select value={settings.matchDuration} onChange={(e) => set('matchDuration', e.target.value as ProjectSettings['matchDuration'])}>
            <option value="preferVideo">{t('Ưu tiên video gốc', 'Prioritize original video')}</option>
            <option value="none">{t('Giữ nguyên TTS', 'Keep TTS intact')}</option>
            <option value="natural">{t('Tự nhiên, rút gọn nhẹ', 'Natural, slightly shortened')}</option>
            <option value="stretch">{t('Kéo giãn khớp đoạn', 'Stretch to match the cue')}</option>
          </select>
        </label>

        <label>
          <span>{t('Giọng mặc định', 'Default voice')}</span>
          <select
            value={voices.some((voice) => voice.id === settings.defaultVoice) ? settings.defaultVoice : (voices[0]?.id || '')}
            disabled={voices.length === 0}
            onChange={(e) => set('defaultVoice', e.target.value)}
          >
            {voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}
          </select>
        </label>

        <label>
          <span>{t('Phụ đề', 'Captions')}</span>
          <select value={captionMode} onChange={(e) => setCaptionMode(e.target.value)}>
            <option value="cover">{t('Che chữ cũ + chèn bản dịch', 'Cover old text + add translation')}</option>
            <option value="below">{t('Chèn bản dịch phía dưới', 'Place translation below')}</option>
            <option value="above">{t('Chèn bản dịch phía trên', 'Place translation above')}</option>
            <option value="none">{t('Không chèn chữ dịch', 'Do not burn translated text')}</option>
          </select>
        </label>

        <label className="blur-band-label">
          <span>{t('Vùng làm mờ cố định', 'Persistent blur zone')}</span>
          <select
            id="blurBandMode"
            value={settings.blurBandMode ?? 'auto'}
            onChange={(e) => set('blurBandMode', e.target.value as 'off' | 'auto' | 'manual')}
          >
            <option value="off">{t('Tắt', 'Off')}</option>
            <option value="auto">{t('Tự động (phát hiện qua OCR)', 'Auto (OCR-detected)')}</option>
            <option value="manual">{t('Thủ công (kéo chọn trong preview)', 'Manual (drag in preview)')}</option>
          </select>
          {settings.blurBandMode === 'manual' && (
            <small className="blur-band-hint">
              {t(
                'Vào trình chỉnh sửa → kéo khung "Vùng làm mờ" để định vị.',
                'Go to the editor → drag the "Blur zone" frame to position it.',
              )}
            </small>
          )}
        </label>

        <label>
          <span>{t('Cỡ chữ', 'Font size')}</span>
          <select
            value={String(settings.subtitleFontSize)}
            disabled={!settings.burnSubs}
            onChange={(e) => set('subtitleFontSize', Number(e.target.value))}
          >
            <option value="0">{t('Tự động (khuyên dùng)', 'Auto (recommended)')}</option>
            {fontSizeOptions.map((size) => <option key={size} value={size}>{size} px</option>)}
          </select>
        </label>

        <label>
          <span>{t('Số luồng', 'Workers')}</span>
          <select value={String(settings.workers)} onChange={(e) => set('workers', Number(e.target.value))}>
            <option value="0">{t('Tự động', 'Auto')}</option>
            {[1, 2, 4, 6, 8, 12, 16].map((count) => <option key={count} value={count}>{count}</option>)}
          </select>
        </label>

        <label>
          <span>{t('Độ phân giải xuất', 'Export resolution')}</span>
          <select
            value={settings.exportResolution}
            onChange={(e) => set('exportResolution', e.target.value as ProjectSettings['exportResolution'])}
          >
            <option value="original">{t('Giữ nguyên', 'Original')}</option>
            {['480', '720', '1080', '1440', '2160'].map((size) => <option key={size} value={size}>{size}p</option>)}
          </select>
        </label>
      </div>

      {settings.translator === 'ollama' ? (
        <div className="clone-settings-grid clone-settings-subgroup">
          <label>
            <span>Ollama</span>
            <select value={settings.ollamaMode} onChange={(e) => set('ollamaMode', e.target.value as ProjectSettings['ollamaMode'])}>
              <option value="cloud">Cloud Free</option>
              <option value="local">Local</option>
            </select>
          </label>
          {settings.ollamaMode === 'cloud' ? (
            <label>
              <span>{t('Model Cloud', 'Cloud model')}</span>
              <input value={settings.ollamaModel} onChange={(e) => set('ollamaModel', e.target.value || 'minimax-m3:cloud')} />
            </label>
          ) : (
            <label>
              <span>{t('Model', 'Model')}</span>
              <select value={ollamaLocalModels.includes(settings.ollamaModel) ? settings.ollamaModel : ''} onChange={(e) => set('ollamaModel', e.target.value)}>
                {!ollamaLocalModels.length && <option value="">{t('Chưa tìm thấy model Ollama', 'No Ollama model found')}</option>}
                {ollamaLocalModels.map((model) => <option key={model} value={model}>{model}</option>)}
              </select>
            </label>
          )}
        </div>
      ) : null}

      <div className="clone-settings-toggles">
        <label className="check">
          <input type="checkbox" checked={settings.coverLogo} onChange={(e) => set('coverLogo', e.target.checked)} />
          {t('Che Logo', 'Remove logo')}
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={Boolean(settings.speakerDiarization)}
            disabled={settings.engine !== 'whisper'}
            onChange={(e) => set('speakerDiarization', e.target.checked)}
          />
          {t('Tách người nói', 'Separate speakers')}
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={settings.processOriginalAudio}
            onChange={(e) => onChange({
              ...settings,
              processOriginalAudio: e.target.checked,
              originalAudioMode: e.target.checked && settings.originalAudioMode === 'original'
                ? 'no_vocals'
                : settings.originalAudioMode,
            })}
          />
          {t('Lọc âm thanh gốc', 'Filter original audio')}
        </label>
      </div>

      {settings.speakerDiarization && settings.engine === 'whisper' ? (
        <div className="clone-settings-inline">
          <label>
            <span>{t('Số người nói', 'Speaker count')}</span>
            <select value={settings.speakerCount || 0} onChange={(e) => set('speakerCount', Number(e.target.value))}>
              <option value={0}>{t('Tự phát hiện', 'Auto detect')}</option>
              {[2, 3, 4, 5, 6, 7, 8].map((count) => <option key={count} value={count}>{count}</option>)}
            </select>
          </label>
        </div>
      ) : null}

      {settings.processOriginalAudio ? (
        <div className="clone-settings-inline">
          <label>
            <span>{t('Chế độ âm thanh gốc', 'Original audio mode')}</span>
            <select value={settings.originalAudioMode} onChange={(e) => set('originalAudioMode', e.target.value as ProjectSettings['originalAudioMode'])}>
              <option value="no_vocals">{t('Xóa lời', 'Remove vocals')}</option>
              <option value="vocals">{t('Chỉ giữ lời', 'Keep vocals only')}</option>
              <option value="original">{t('Giữ âm gốc', 'Keep original audio')}</option>
              <option value="mute">{t('Tắt âm gốc', 'Mute original audio')}</option>
            </select>
          </label>
          <label>
            <span>{t('Âm lượng nền', 'Background volume')} · {settings.originalAudioVolume}%</span>
            <input
              type="range"
              min={0}
              max={100}
              value={Math.max(0, Math.min(100, settings.originalAudioVolume))}
              disabled={settings.originalAudioMode === 'mute'}
              onChange={(e) => set('originalAudioVolume', Number(e.target.value))}
            />
          </label>
        </div>
      ) : null}
    </section>
  )
}
