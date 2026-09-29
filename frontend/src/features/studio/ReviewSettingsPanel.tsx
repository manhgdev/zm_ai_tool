import { useEffect, useState, type ReactNode } from 'react'
import { localize, useLocale } from '@/app/i18n'
import { api } from '@/features/project/project.api'
import {
  GENRES, KEEP_SKIP_PRESETS, loadPacks, savePacks,
  type CaptionMode, type Pack, type ReviewCloudProvider, type ReviewSettings,
} from './reviewSettings'
import { studioApi } from './studio.api'

type Setter = (patch: Partial<ReviewSettings>) => void

export function useVoices(language: string) {
  const [voices, setVoices] = useState<{ id: string; name: string }[]>([])
  useEffect(() => {
    api.voices(language).then((rows) => setVoices(rows || [])).catch(() => setVoices([]))
  }, [language])
  return voices
}

export function ReviewLangFields({ settings, onChange }: { settings: ReviewSettings; onChange: Setter }) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  return (
    <>
      <div className="rv-nest">
        <h3 className="rv-nest-h">{t('Nhận dạng', 'Recognition')}</h3>
        <div className="rv-choices">
          <button type="button" className={`rv-choice${settings.recognitionEngine === 'whisper' ? ' on' : ''}`} onClick={() => onChange({ recognitionEngine: 'whisper' })}>
            <b>Whisper</b>
            <small>{t('Chạy trên máy, ưu tiên phụ đề có sẵn rồi mới nhận dạng giọng nói.', 'Runs on this machine; prefers existing subtitles before speech recognition.')}</small>
          </button>
          <button type="button" className={`rv-choice${settings.recognitionEngine === 'capcut' ? ' on' : ''}`} onClick={() => onChange({ recognitionEngine: 'capcut' })}>
            <b>{t('CapCut cloud', 'CapCut cloud')}</b>
            <small>{t('Gửi video lên CapCut để nhận dạng; không chạy Whisper.', 'Uploads the video to CapCut for recognition; Whisper is not used.')}</small>
          </button>
        </div>
        {settings.recognitionEngine === 'capcut' ? <p className="rv-hint">{t('Cần mạng. Transcript được cache riêng theo CapCut để không lẫn với Whisper.', 'Requires internet. The transcript is cached separately from Whisper.')}</p> : null}
      </div>
      <div className="rv-two">
        <label className="rv-field rv-lang">
          <span>{t('Ngôn ngữ gốc', 'Original language')}</span>
          <select value={settings.sourceLang} onChange={(e) => onChange({ sourceLang: e.target.value })}>
            <option value="auto">{t('Tự động', 'Auto')}</option>
            <option value="zh">中文</option>
            <option value="en">English</option>
            <option value="ja">日本語</option>
            <option value="ko">한국어</option>
            <option value="vi">{t('Tiếng Việt', 'Vietnamese')}</option>
          </select>
        </label>
        <label className="rv-field rv-lang">
          <span>{t('Ngôn ngữ thoại', 'Spoken language')}</span>
          <select value={settings.language} onChange={(e) => onChange({ language: e.target.value })}>
            <option value="vi">{t('Tiếng Việt', 'Vietnamese')}</option>
            <option value="en">English</option>
            <option value="zh">中文</option>
            <option value="ja">日本語</option>
            <option value="ko">한국어</option>
          </select>
        </label>
      </div>
      <p className="rv-hint">{t(
        'Ngôn ngữ gốc là tiếng phim. Ngôn ngữ thoại là lời kể TTS và caption.',
        'The source language is the movie language. The spoken language is used for narration TTS and captions.',
      )}</p>
    </>
  )
}

export function Stepper({ value, min = 0, max = 999, onChange }: { value: number; min?: number; max?: number; onChange: (v: number) => void }) {
  return (
    <div className="rv-stepper">
      <button type="button" onClick={() => onChange(Math.max(min, value - 1))}>‹</button>
      <span>{value}</span>
      <button type="button" onClick={() => onChange(Math.min(max, value + 1))}>›</button>
    </div>
  )
}

export function ReviewLeftPanel({ settings, onChange }: { settings: ReviewSettings; onChange: Setter }) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const smartCutHint = t(
    'Cắt thông minh cần video có sẵn trên máy. Hãy bấm "Chọn video", hoặc tải video từ link về trước ở tab Tải video.',
    'Smart cut needs a video already on disk. Click "Choose video", or download the link first from Download Video.',
  )

  const mode = settings.buildMode
  return (
    <>
      <div className="rv-nest">
        <h3 className="rv-nest-h">{t('Chế độ dựng & Đồng bộ', 'Build & sync mode')}</h3>
        <p className="rv-hint">{t('Chọn phương thức ghép hình ảnh và âm thanh khi xuất video thành phẩm.', 'Choose how picture and voice are combined when exporting the final video.')}</p>

        <div className="rv-sec">
          <span className="rv-sec-l"><i className="rv-dot orange" />{t('Chế độ bám nhả theo hình', 'Beat-follow mode')}</span>
          <span className="rv-slow">● {t('Ưu tiên khớp cảnh', 'Prioritizes scene matching')}</span>
        </div>
        <div className="rv-choices">
          <button type="button" className={`rv-choice${mode === 'fixed' ? ' on' : ''}`} onClick={() => onChange({ buildMode: 'fixed' })}>
            <b>{t('Khung hình cố định', 'Fixed frames')}</b>
            <span>{t('Bám mô cảnh theo nhịp đều để nề bản nguyên tốt. Xử lý chậm hơn do cắt ghép nhiều cảnh.', 'Keeps scene beats even, staying close to the source. Slower due to more cuts.')}</span>
          </button>
          <button type="button" className={`rv-choice${mode === 'stretch' ? ' on' : ''}`} onClick={() => onChange({ buildMode: 'stretch' })}>
            <b>{t('Co giãn hình theo giọng', 'Stretch picture to voice')}</b>
            <span>{t('Bám mô cảnh và co giãn khung hình theo giọng đọc. Nề bản nguyên tốt, xử lý lâu hơn.', 'Follows scenes and stretches frames to match narration. More original, takes longer.')}</span>
          </button>
        </div>

        <div className="rv-sec rv-sec-div">
          <span className="rv-sec-l"><i className="rv-dot orange" />{t('Chế độ cắt thông minh', 'Smart cut mode')}</span>
          <span className="rv-fast">● {t('Dựng theo từng phần', 'Builds in parts')}</span>
        </div>
        <div className="rv-choices">
          <button type="button" className={`rv-choice${mode === 'accumulate' ? ' on' : ''}`} onClick={() => onChange({ buildMode: 'accumulate' })}>
            <b>{t('Phân đoạn tích lũy', 'Cumulative segments')}</b>
            <span>{t('Phân tích phim một lần, rồi dựng các phần nối tiếp theo ngữ cảnh.', 'Analyzes the film once, then builds consecutive contextual parts.')}</span>
          </button>
          <button type="button" className={`rv-choice${mode === 'smart' ? ' on' : ''}`} title={smartCutHint} onClick={() => onChange({ buildMode: 'smart' })}>
            <b>{t('Cắt thông minh (Nâng cao)', 'Smart cut (Advanced)')} <em className="rv-pill soft">{t('Thử nghiệm', 'Experimental')}</em></b>
            <span>{t('AI tự chọn cảnh đắt giá, giữ một khoảng bỏ một khoảng.', 'AI keeps high-value stretches and skips the rest.')}</span>
          </button>
        </div>
      </div>

      {mode === 'smart' ? (
        <>
          <h4 className="rv-nest-h small">{t('Nhịp cắt video', 'Cut rhythm')}</h4>
          <p className="rv-hint">{t('Giữ một khoảng, bỏ một khoảng', 'Keep a stretch, skip a stretch')}</p>
          <div className="rv-two">
            <label className="rv-field">
              <span>{t('Giữ lại (giây)', 'Keep (sec)')}</span>
              <Stepper value={settings.keepSec} min={1} max={30} onChange={(v) => onChange({ keepSec: v })} />
            </label>
            <label className="rv-field">
              <span>{t('Bỏ qua (giây)', 'Skip (sec)')}</span>
              <Stepper value={settings.skipSec} min={1} max={60} onChange={(v) => onChange({ skipSec: v })} />
            </label>
          </div>
          <div className="rv-presets">
            {KEEP_SKIP_PRESETS.map((p) => (
              <button
                key={`${p.keep}-${p.skip}`}
                type="button"
                className={`rv-preset${settings.keepSec === p.keep && settings.skipSec === p.skip ? ' on' : ''}`}
                onClick={() => onChange({ keepSec: p.keep, skipSec: p.skip })}
              >
                {p.keep}s - {p.skip}s
              </button>
            ))}
          </div>
        </>
      ) : mode === 'accumulate' ? (
        <label className="rv-field">
          <div className="rv-inline">
            <span className="rv-lab">{t('Thời lượng review mong muốn (phút)', 'Preferred review length (minutes)')}</span>
            <div className="rv-seg">
              {([10, 15, 20] as const).map((n) => (
                <button key={n} type="button" className={settings.chunkMinutes === n ? 'on' : undefined} onClick={() => onChange({ chunkMinutes: n })}>{n}p</button>
              ))}
            </div>
          </div>
          <p className="rv-hint">{t('Ưu tiên mạch review tự nhiên và thông tin có căn cứ; thời lượng là mục tiêu, không kéo dài bằng lời hoặc cảnh đệm.', 'Prioritizes a natural, evidence-grounded review; length is a target, never padded with filler narration or footage.')}</p>
        </label>
      ) : mode === 'fixed' ? (
        <label className="rv-field">
          <div className="rv-inline">
            <span className="rv-lab">{t('Độ dài video review (phút)', 'Review length (minutes)')}</span>
            <div className="rv-seg">
              {([10, 15, 20] as const).map((n) => (
                <button key={n} type="button" className={settings.chunkMinutes === n ? 'on' : undefined} onClick={() => onChange({ chunkMinutes: n })}>{n}p</button>
              ))}
            </div>
          </div>
          <p className="rv-hint">{t('Một video duy nhất — không chia thành nhiều phần.', 'One video — not split into parts.')}</p>
        </label>
      ) : null}

      {/* Nhịp nghỉ giữa câu — applies to all build modes */}
      <h4 className="rv-nest-h small">{t('Nhịp nghỉ giữa câu', 'Pause between sentences')}</h4>
      <p className="rv-hint">{t('Khoảng lặng giữa các câu đọc – ảnh hưởng tốc độ tổng thể video.', 'Silence between spoken sentences — affects overall video pacing.')}</p>
      <div className="rv-choices three">
        <button type="button" className={`rv-choice center${settings.pausePace === 'fast' ? ' on' : ''}`} onClick={() => onChange({ pausePace: 'fast' })}>
          <span className="rv-ico">⚡</span><b>{t('Nhanh', 'Fast')}</b><small>{t('Video ngắn - Reels', 'Short video - Reels')}</small>
        </button>
        <button type="button" className={`rv-choice center${settings.pausePace === 'balanced' ? ' on' : ''}`} onClick={() => onChange({ pausePace: 'balanced' })}>
          <span className="rv-ico">⚖️</span><b>{t('Cân bằng', 'Balanced')}</b><small>{t('Tối ưu cho review', 'Optimized for review')}</small>
        </button>
        <button type="button" className={`rv-choice center${settings.pausePace === 'slow' ? ' on' : ''}`} onClick={() => onChange({ pausePace: 'slow' })}>
          <span className="rv-ico">🐢</span><b>{t('Chậm rãi', 'Slow')}</b><small>{t('Kể chuyện sâu', 'Deep storytelling')}</small>
        </button>
      </div>
      {mode === 'stretch' ? (
        <p className="rv-hint">{t('Ở chế độ Co giãn, độ dài cảnh tự điều chỉnh theo TTS – không cần chỉnh nhịp cắt thủ công.', 'In Stretch mode, scene length auto-adjusts to TTS — no manual cut timing needed.')}</p>
      ) : null}
    </>
  )
}

export function AudioSlider({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  return (
    <label className="rv-field">
      <div className="rv-inline">
        <span className="rv-lab">{t(`Âm thanh phim gốc · ${value}%`, `Original movie audio · ${value}%`)}</span>
        <button type="button" className="rv-mini" onClick={() => onChange(value > 0 ? 0 : 18)}>{value > 0 ? t('Tắt', 'Mute') : t('Bật', 'On')}</button>
      </div>
      <input className="rv-range" type="range" min={0} max={80} value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </label>
  )
}

export function CaptionModePicker({ value, onChange }: { value: CaptionMode; onChange: (v: CaptionMode) => void }) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const modes: Array<{ id: CaptionMode; label: string; hint: string }> = [
    { id: 'off', label: t('Tắt', 'Off'), hint: t('Không chèn hoặc che phụ đề', 'Do not insert or cover subtitles') },
    { id: 'cover', label: t('Che chữ cũ + chèn dịch', 'Cover old words + insert translation'), hint: t('Định vị OCR rồi che chữ gốc trên phim', 'OCR locates and covers original on-screen text') },
    { id: 'below', label: t('Chèn dịch phía dưới', 'Insert translation below'), hint: t('Caption lời kể nằm phía dưới khung', 'Narration captions sit at the bottom') },
    { id: 'above', label: t('Chèn dịch phía trên', 'Insert translation above'), hint: t('Caption lời kể nằm phía trên chữ gốc', 'Narration captions sit above the original text') },
  ]
  return (
    <div className="rv-field">
      <span className="rv-lab">{t('Chèn phụ đề', 'Insert subtitles')}</span>
      <p className="rv-hint">{t('Caption theo ngôn ngữ thoại đã chọn. Dùng định vị OCR của Clone — không tải model mới.', 'Captions follow the selected spoken language. Uses Clone OCR locate — no new models.')}</p>
      <div className="rv-choices">
        {modes.map((m) => (
          <button key={m.id} type="button" className={`rv-choice center${value === m.id ? ' on' : ''}`} onClick={() => onChange(m.id)}>
            <b>{m.label}</b>
            <small>{m.hint}</small>
          </button>
        ))}
      </div>
    </div>
  )
}

export function ReviewRightPanel({
  settings, onChange, voices, seriesSlot, projectAssetsHint,
}: {
  settings: ReviewSettings
  onChange: Setter
  voices: { id: string; name: string }[]
  seriesSlot?: ReactNode
  projectAssetsHint?: string
}) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [packs, setPacks] = useState<Pack[]>(loadPacks)
  const [packOpen, setPackOpen] = useState(false)
  const [packEdit, setPackEdit] = useState<Pack | null>(null)
  const [ollamaModels, setOllamaModels] = useState<string[]>([])
  const [cloudModels, setCloudModels] = useState<{ id: string; label: string; free: boolean }[]>([])
  const [cloudModelsLoading, setCloudModelsLoading] = useState(false)

  useEffect(() => {
    if (settings.reviewMode !== 'llm') return
    studioApi.diagnostics()
      .then((data) => setOllamaModels(data.ollamaModels || []))
      .catch(() => setOllamaModels([]))
  }, [settings.reviewMode])

  useEffect(() => {
    if (settings.reviewMode !== 'cloud') return
    let cancelled = false
    setCloudModelsLoading(true)
    fetch(`/api/chat/models?provider=${encodeURIComponent(settings.reviewProvider)}&includePaid=true`)
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
        if (!cancelled) setCloudModels(rows)
      })
      .catch(() => { if (!cancelled) setCloudModels([]) })
      .finally(() => { if (!cancelled) setCloudModelsLoading(false) })
    return () => { cancelled = true }
  }, [settings.reviewMode, settings.reviewProvider])

  useEffect(() => {
    if (settings.reviewMode !== 'cloud' || !cloudModels.length) return
    if (cloudModels.some((model) => model.id === settings.reviewCloudModel)) return
    onChange({ reviewCloudModel: (cloudModels.find((model) => model.free) || cloudModels[0]).id })
  }, [cloudModels, onChange, settings.reviewCloudModel, settings.reviewMode])

  function openPacks() {
    setPackEdit(packs.find((p) => p.id === settings.genre) || packs[0])
    setPackOpen(true)
  }

  function savePack() {
    if (!packEdit) return
    const next = packs.some((p) => p.id === packEdit.id) ? packs.map((p) => (p.id === packEdit.id ? packEdit : p)) : [...packs, packEdit]
    setPacks(next)
    savePacks(next)
    onChange({ genre: packEdit.id })
    setPackOpen(false)
  }

  const genreOptions = [
    // Built-in genres (merged with their pack names if overridden)
    ...GENRES.map((g) => {
      const overridePack = packs.find((p) => p.id === g.id)
      return { id: g.id, label: overridePack?.name || (locale === 'en' ? g.en : g.vi) }
    }),
    // Custom packs not in GENRES
    ...packs.filter((p) => !GENRES.some((g) => g.id === p.id)).map((p) => ({ id: p.id, label: p.name || p.id })),
  ]

  return (
    <>
      <div className="rv-tts-head">
        <span className="rv-lab">{t('Giọng đọc (TTS)', 'Narration (TTS)')}</span>
        <span className="rv-lab rv-lab-r">
          {t('Thể loại phim', 'Movie genre')}{' '}
          <button type="button" className="rv-link" onClick={openPacks}>{t('Cấu hình…', 'Configure…')}</button>
        </span>
      </div>
      <div className="rv-two">
        <div className="rv-voice">
          <select value={settings.voice} onChange={(e) => onChange({ voice: e.target.value })}>
            <option value="system">{t('Giọng hệ thống', 'System voice')}</option>
            {voices.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
          </select>
        </div>
        <div className="rv-voice">
          <select value={settings.genre} onChange={(e) => onChange({ genre: e.target.value })}>
            {genreOptions.map((g) => <option key={g.id} value={g.id}>{g.label}</option>)}
          </select>
        </div>
      </div>

      {seriesSlot}

      <label className="rv-field">
        <span className="rv-lab">{t('Ghi chú thêm (Tùy chọn)', 'Extra notes (optional)')}</span>
        <input value={settings.notes} onChange={(e) => onChange({ notes: e.target.value })} placeholder={t('Ví dụ: Gọi tên nhân vật chính là "Anh Hời"', 'e.g. Call the lead character "Anh Hời"')} />
      </label>

      <h4 className="rv-nest-h small">{t('Văn phong kịch bản', 'Script style')}</h4>
      <div className="rv-style">
        {([
          ['chuan', '🎬', t('Chuẩn', 'Standard'), t('Trung tính, mạch lạc', 'Neutral, clear')],
          ['reviewer', '✎', 'Reviewer', t('Cá tính, cuốn hút hơn', 'More personality')],
          ['storytelling', '🔥', 'Storytelling', t('Kịch tính, mượt mà hơn', 'More dramatic, smoother')],
          ['cinematic', '🎭', 'Cinematic', t('Lời kể liền mạch, giàu chất điện ảnh', 'Flowing, cinematic narration')],
        ] as const).map(([id, ico, label, hint]) => (
          <button key={id} type="button" className={`rv-choice center${settings.scriptStyle === id ? ' on' : ''}`} onClick={() => onChange({ scriptStyle: id })}>
            <span className="rv-ico">{ico}</span><b>{label}</b><small>{hint}</small>
          </button>
        ))}
      </div>

      <h4 className="rv-nest-h small">{t('Chế độ biên soạn', 'Writing mode')}</h4>
      <div className="rv-choices">
        <button type="button" className={`rv-choice${settings.reviewMode === 'llm' ? ' on' : ''}`} onClick={() => onChange({ reviewMode: 'llm' })}>
          <b>{t('AI recap (Ollama)', 'AI recap (Ollama)')}</b>
          <span>{t('AI phân tích các block cảnh rồi viết lời review theo mạch phim.', 'AI analyzes scene blocks and writes a chronological recap.')}</span>
        </button>
        <button type="button" className={`rv-choice${settings.reviewMode === 'cloud' ? ' on' : ''}`} onClick={() => onChange({ reviewMode: 'cloud' })}>
          <b>{t('Review · AI Cloud', 'Review · Cloud AI')}</b>
          <span>{t('Gemini biên tập mạch truyện từ timeline SRT/thoại có timecode; không gửi khung hình.', 'Gemini edits a story arc from timed SRT/speech; no video frames are uploaded.')}</span>
        </button>
        <button type="button" className={`rv-choice${settings.reviewMode === 'translate' ? ' on' : ''}`} onClick={() => onChange({ reviewMode: 'translate' })}>
          <b>{t('Dịch tuần tự', 'Sequential translation')}</b>
          <span>{t('Không cần Ollama: dịch lần lượt phụ đề/thoại nguồn, không diễn giải thêm.', 'No Ollama: translate source subtitles/speech in order without extra narration.')}</span>
        </button>
      </div>
      {settings.reviewMode === 'llm' ? (
        <label className="rv-field">
          <span className="rv-lab">{t('Model Ollama', 'Ollama model')}</span>
          <select value={settings.reviewModel} onChange={(e) => onChange({ reviewModel: e.target.value })}>
            <option value="auto">{t('Tự động chọn', 'Auto select')}</option>
            {ollamaModels.map((model) => <option key={model} value={model}>{model}</option>)}
          </select>
          <p className="rv-hint">{ollamaModels.length
            ? t('Model này dùng để phân tích block cảnh và viết recap.', 'This model analyzes scene blocks and writes the recap.')
            : t('Không tìm thấy model Ollama. Mở Ollama hoặc chọn chế độ Dịch tuần tự.', 'No Ollama model found. Start Ollama or use Sequential translation.')}
          </p>
        </label>
      ) : settings.reviewMode === 'cloud' ? (
        <div className="rv-field">
          <div className="rv-two">
            <label className="rv-field">
              <span className="rv-lab">{t('Cloud AI', 'Cloud AI')}</span>
              <select value={settings.reviewProvider} onChange={(e) => {
                const provider = e.target.value as ReviewCloudProvider
                onChange({ reviewProvider: provider, reviewCloudModel: '' })
              }}>
                <option value="gemini">Gemini</option>
                <option value="grok">Grok</option>
                <option value="openai">OpenAI</option>
                <option value="deepseek">DeepSeek</option>
                <option value="openrouter">OpenRouter</option>
                <option value="groq">Groq</option>
                <option value="nvidia">NVIDIA NIM</option>
                <option value="mistral">Mistral</option>
              </select>
            </label>
            <label className="rv-field">
              <span className="rv-lab">Model</span>
              <select value={settings.reviewCloudModel} disabled={cloudModelsLoading || !cloudModels.length} onChange={(e) => onChange({ reviewCloudModel: e.target.value })}>
                {!cloudModels.length && <option value="">{t('Chưa tải được model', 'No model list loaded')}</option>}
                {cloudModels.some((model) => model.free) && <optgroup label={t('Model free dùng được', 'Free models available')}>
                  {cloudModels.filter((model) => model.free).map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}
                </optgroup>}
                {cloudModels.some((model) => !model.free) && <optgroup label={t('Model cần credit', 'Models requiring credit')}>
                  {cloudModels.filter((model) => !model.free).map((model) => <option key={model.id} value={model.id}>{model.label} — {t('cần credit', 'credit required')}</option>)}
                </optgroup>}
              </select>
            </label>
          </div>
          <p className="rv-hint">{cloudModelsLoading
            ? t('Đang tải model live…', 'Loading live models…')
            : settings.reviewCloudModel && cloudModels.some((model) => model.id === settings.reviewCloudModel && !model.free)
              ? t('Model đang chọn cần credit.', 'The selected model requires credit.')
              : t('Model free được hiển thị ở nhóm đầu.', 'Free models are shown in the first group.')}</p>
          <p className="rv-hint">{t(
            'Model được chọn riêng cho project Review này. API key và Base URL vẫn lấy từ Cấu hình → Cloud; key không được lưu trong job.',
            'The model is selected for this Review project. The API key and base URL still come from Settings → Cloud; keys are never stored in the job.',
          )}</p>
        </div>
      ) : null}

      <h4 className="rv-nest-h small">{t('Độ dài lời kể', 'Narration length')}</h4>
      <div className="rv-narr">
        <button type="button" className={`rv-choice${settings.narration === 'default' ? ' on' : ''}`} onClick={() => onChange({ narration: 'default' })}>
          <b>{t('Mặc định', 'Default')}</b><span>{t('Độ dài tiêu chuẩn, an toàn nhất', 'Standard length, safest')}</span>
        </button>
        <button type="button" className={`rv-choice${settings.narration === 'mild' ? ' on' : ''}`} onClick={() => onChange({ narration: 'mild' })}>
          <b>{t('Tăng nhẹ', 'Slightly longer')}</b><span>{t('Thêm ~20% chữ, giọng đọc hơi nhanh hơn', '~20% more text, slightly faster narration')}</span>
        </button>
        <button type="button" className={`rv-choice${settings.narration === 'more' ? ' on' : ''}`} onClick={() => onChange({ narration: 'more' })}>
          <b>{t('Tăng nhiều', 'Much longer')}</b><span>{t('Thêm ~37% chữ, giọng đọc nhanh hơn rõ rệt', '~37% more text, noticeably faster narration')}</span>
        </button>
      </div>
      <p className="rv-hint">{t('Độ dài lời kể mặc định giúp đảm bảo khớp đúng độ dài audio đọc theo tốc độ đọc thật của giọng lồng tiếng – cho chuyển động hình tự nhiên nhất. Muốn tự chỉnh/giảm tốc độ đọc, hãy chọn chế độ Nhanh.', 'Default narration length keeps audio duration matched to the real narration speed for the most natural picture pacing. To adjust reading speed, use the Fast mode.')}</p>

      <details className="rv-assets">
        <summary>{t('Project Assets', 'Project Assets')}</summary>
        <p>{projectAssetsHint || t('Không có tệp asset nào', 'No asset files yet')}</p>
      </details>

      {packOpen && packEdit ? (
        <div className="rv-modal" onClick={() => setPackOpen(false)}>
          <div className="rv-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="rv-card-title">
              <h2>{t('Bộ phong cách lời kể', 'Narration style packs')}</h2>
              <button type="button" className="rv-ghost" onClick={() => setPackOpen(false)}>×</button>
            </div>
            <p className="rv-hint">{t('Chọn một mẫu hoặc tự chỉnh lại cách biên soạn', 'Pick a preset or edit how the script is written')}</p>
            <div className="rv-packs-head">
              <span className="rv-lab">{packs.length} {t('phong cách đã lưu', 'saved styles')}</span>
              <button type="button" className="rv-run small" onClick={() => setPackEdit({ id: `pack_${Date.now()}`, name: '', hint: '', rules: '' })}>+ {t('Thêm phong cách mới', 'Add style')}</button>
            </div>
            <div className="rv-packs">
              {packs.map((p) => (
                <button key={p.id} type="button" className={`rv-choice${packEdit.id === p.id ? ' on' : ''}`} onClick={() => setPackEdit(p)}>
                  <b>{p.name}</b>
                </button>
              ))}
            </div>
            <label className="rv-field">
              <span className="rv-lab">{t('Tên phong cách', 'Style name')}</span>
              <input value={packEdit.name} disabled={packEdit.locked} onChange={(e) => setPackEdit({ ...packEdit, name: e.target.value })} />
              {packEdit.locked ? <small className="rv-hint">{t(`Mã phong cách: ${packEdit.id} (Không thể chỉnh sửa - không thể xóa)`, `Style id: ${packEdit.id} (locked - cannot delete)`)}</small> : null}
            </label>
            <label className="rv-field">
              <span className="rv-lab">{t('Quy tắc biên soạn & Giọng thoại', 'Writing rules & voice')}</span>
              <textarea value={packEdit.rules} onChange={(e) => setPackEdit({ ...packEdit, rules: e.target.value })} />
            </label>
            <div className="rv-dialog-actions">
              <button type="button" className="rv-ghost" onClick={() => setPackOpen(false)}>{t('Đóng', 'Close')}</button>
              <button type="button" className="rv-run" disabled={packEdit.locked} onClick={savePack}>{t('Lưu thay đổi', 'Save changes')}</button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  )
}
