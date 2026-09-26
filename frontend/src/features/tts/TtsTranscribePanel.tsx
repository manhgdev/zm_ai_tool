/** Chép lời — Whisper / CapCut / OCR / phụ đề → text cho TTS Studio. */
import { useMemo, useState, type DragEvent } from 'react'
import { localize, useLocale } from '@/app/i18n'
import { IconUpload } from './TtsIcons'

export type TranscribeEngine = 'whisper' | 'capcut' | 'paddleocr' | 'subtitle'

type Props = {
  file: File | null
  lang: string
  engine: TranscribeEngine
  busy: boolean
  resultText: string
  onFileChange: (file: File | null) => void
  onLangChange: (lang: string) => void
  onEngineChange: (engine: TranscribeEngine) => void
  onSubmit: () => void
  onResultChange: (text: string) => void
  onApplyToTts: () => void
}

export default function TtsTranscribePanel({
  file,
  lang,
  engine,
  busy,
  resultText,
  onFileChange,
  onLangChange,
  onEngineChange,
  onSubmit,
  onResultChange,
  onApplyToTts,
}: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [isDragging, setIsDragging] = useState(false)

  const accept = useMemo(() => {
    if (engine === 'subtitle') return '.srt,.vtt,text/vtt,application/x-subrip'
    if (engine === 'paddleocr') return 'video/*,.mp4,.mkv,.webm,.mov,.avi,.m4v'
    return 'audio/*,video/*,.wav,.mp3,.m4a,.flac,.ogg,.mp4,.mkv,.webm,.mov'
  }, [engine])

  const dropHint = useMemo(() => {
    if (engine === 'subtitle') return t('File phụ đề .srt / .vtt', 'Subtitle file .srt / .vtt')
    if (engine === 'paddleocr') return t('Video có chữ trên màn hình', 'Video with on-screen text')
    if (engine === 'capcut') return t('Audio / video — CapCut cloud', 'Audio / video — CapCut cloud')
    return t('Audio / video — Whisper local', 'Audio / video — Whisper local')
  }, [engine, t])

  const engineHint = useMemo(() => {
    if (engine === 'subtitle') {
      return t('Đọc text từ file phụ đề (không chạy ASR/OCR).', 'Read text from a subtitle file (no ASR/OCR).')
    }
    if (engine === 'paddleocr') {
      return t('OCR chữ hardsub trên khung hình video (RapidOCR).', 'OCR hardsub text from video frames (RapidOCR).')
    }
    if (engine === 'capcut') {
      return t('Nhận dạng giọng nói qua CapCut cloud.', 'Speech recognition via CapCut cloud.')
    }
    return t('Nhận dạng giọng nói local bằng Faster-Whisper.', 'Local speech recognition with Faster-Whisper.')
  }, [engine, t])

  return (
    <div className="tts-page-panel">
      <section className="tts-card" id="tts-transcribe">
        <h3 className="tts-card-title">
          <span className="tts-step">1</span> {t('Chép lời', 'Transcribe')}
        </h3>
        <p style={{ margin: '0 0 12px', fontSize: '0.8rem', color: 'var(--tts-muted)', lineHeight: 1.6 }}>
          {t(
            'Chọn nguồn nhận dạng giống Clone Video (Whisper / CapCut / OCR / phụ đề), rồi đưa text vào Tạo giọng nói.',
            'Pick a recognition source like Clone Video (Whisper / CapCut / OCR / subtitles), then send text to Create voice.',
          )}
        </p>

        <label className="tts-field" style={{ marginBottom: 12 }}>
          <span>{t('Nguồn nhận dạng', 'Recognition source')}</span>
          <select
            value={engine}
            disabled={busy}
            onChange={(e) => {
              onEngineChange(e.target.value as TranscribeEngine)
              onFileChange(null)
            }}
          >
            <option value="whisper">{t('Giọng nói (Whisper)', 'Speech (Whisper)')}</option>
            <option value="capcut">{t('Giọng nói (CapCut cloud)', 'Speech (CapCut cloud)')}</option>
            <option value="paddleocr">{t('Chữ trên màn (OCR)', 'On-screen text (OCR)')}</option>
            <option value="subtitle">{t('Phụ đề SRT', 'Subtitle SRT')}</option>
          </select>
          <span style={{ display: 'block', marginTop: 6, fontSize: '0.72rem', color: 'var(--tts-muted)' }}>
            {engineHint}
          </span>
        </label>

        <label className="tts-field" style={{ marginBottom: 12 }}>
          <span>{t('Ngôn ngữ nguồn', 'Source language')}</span>
          <select value={lang} disabled={busy || engine === 'subtitle'} onChange={(e) => onLangChange(e.target.value)}>
            <option value="auto">{t('Tự động', 'Auto')}</option>
            <option value="vi">{t('Tiếng Việt', 'Vietnamese')}</option>
            <option value="en">{t('Tiếng Anh', 'English')}</option>
            <option value="zh">{t('Tiếng Trung', 'Chinese')}</option>
            <option value="ja">{t('Tiếng Nhật', 'Japanese')}</option>
            <option value="ko">{t('Tiếng Hàn', 'Korean')}</option>
          </select>
        </label>

        <div
          className={`tts-drop${isDragging ? ' is-dragging' : ''}`}
          onDragEnter={(event: DragEvent<HTMLDivElement>) => {
            event.preventDefault()
            event.dataTransfer.dropEffect = 'copy'
            setIsDragging(true)
          }}
          onDragOver={(event: DragEvent<HTMLDivElement>) => {
            event.preventDefault()
            event.dataTransfer.dropEffect = 'copy'
          }}
          onDragLeave={(event: DragEvent<HTMLDivElement>) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setIsDragging(false)
          }}
          onDrop={(event: DragEvent<HTMLDivElement>) => {
            event.preventDefault()
            setIsDragging(false)
            const next = event.dataTransfer.files?.[0]
            if (next) onFileChange(next)
          }}
        >
          <input
            id="tts-transcribe-file"
            type="file"
            accept={accept}
            disabled={busy}
            onChange={(e) => onFileChange(e.target.files?.[0] || null)}
          />
          <label htmlFor="tts-transcribe-file">
            <IconUpload />
            <strong>{file ? file.name : t('Chọn hoặc kéo thả file', 'Choose or drop a file')}</strong>
            <span>{dropHint}</span>
          </label>
        </div>

        <div style={{ marginTop: 14, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button
            type="button"
            className="tts-btn tts-btn-blue"
            disabled={busy || !file}
            onClick={() => onSubmit()}
          >
            {busy ? t('Đang nhận dạng…', 'Transcribing…') : t('Nhận dạng', 'Transcribe')}
          </button>
        </div>
      </section>

      {resultText ? (
        <section className="tts-card" style={{ marginTop: 16 }}>
          <h3 className="tts-card-title">
            <span className="tts-step">2</span> {t('Kết quả', 'Result')}
          </h3>
          <textarea
            className="tts-textarea"
            rows={10}
            value={resultText}
            disabled={busy}
            onChange={(e) => onResultChange(e.target.value)}
          />
          <div style={{ marginTop: 12 }}>
            <button type="button" className="tts-btn tts-btn-blue" disabled={busy || !resultText.trim()} onClick={onApplyToTts}>
              {t('Đưa vào Tạo giọng nói', 'Send to Create voice')}
            </button>
          </div>
        </section>
      ) : null}
    </div>
  )
}
