/** Chép lời — Whisper / CapCut / OCR / phụ đề → text cho TTS Studio. */
import { useEffect, useMemo, useState, type DragEvent } from 'react'
import { PROVIDER_PRESET_MODELS } from '@/features/configuration/configModal.helpers'
import { localize, useLocale } from '@/app/i18n'
import { IconUpload } from './TtsIcons'
import { SRT_STYLE_OPTIONS, srtPreviewLines } from './lib/srt'
import { formatTranscriptSrt, type TranscriptStyle, type TranscriptTimedStyles } from './lib/transcriptSrt'

export type TranscribeEngine = 'whisper' | 'capcut' | 'paddleocr' | 'subtitle'

type Props = {
  file: File | null
  lang: string
  engine: TranscribeEngine
  busy: boolean
  resultText: string
  resultSrt: string
  timedStyles?: TranscriptTimedStyles | null
  onFileChange: (file: File | null) => void
  onLangChange: (lang: string) => void
  onEngineChange: (engine: TranscribeEngine) => void
  onSubmit: () => void
  onResultChange: (text: string) => void
  onSrtChange: (text: string) => void
  onClearCache: () => void
  translation: { enabled: boolean; targetLang: string; translator: string; model: string }
  onTranslationChange: (value: Props['translation']) => void
  translatedText: string
  translatedSrt: string
  onTranslatedTextChange: (value: string) => void
  onTranslatedSrtChange: (value: string) => void
  onApplyToTts: (format: 'txt' | 'srt', content: string) => void
  onRewrite: (text: string) => void
}

export default function TtsTranscribePanel({
  file,
  lang,
  engine,
  busy,
  resultText: sourceText,
  resultSrt: sourceSrt,
  timedStyles,
  translation, onTranslationChange, translatedText, translatedSrt, onTranslatedTextChange, onTranslatedSrtChange,
  onFileChange,
  onLangChange,
  onEngineChange,
  onSubmit,
  onResultChange: onSourceTextChange,
  onSrtChange: onSourceSrtChange,
  onClearCache,
  onApplyToTts,
  onRewrite,
}: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [isDragging, setIsDragging] = useState(false)
  const [resultTab, setResultTab] = useState<'txt' | 'srt'>(() => { try { return localStorage.getItem('tts-result-format') === 'srt' ? 'srt' : 'txt' } catch { return 'txt' } })
  const [variant, setVariant] = useState(() => { try { return localStorage.getItem('tts-result-variant') || 'source' } catch { return 'source' } })
  const isTranslated = translation.enabled && variant === 'translated' && !!translatedText
  const resultText = isTranslated ? translatedText : sourceText
  const resultSrt = isTranslated ? translatedSrt : sourceSrt
  const onResultChange = isTranslated ? onTranslatedTextChange : onSourceTextChange
  const onSrtChange = isTranslated ? onTranslatedSrtChange : onSourceSrtChange
  useEffect(() => { try { localStorage.setItem('tts-result-format', resultTab); localStorage.setItem('tts-result-variant', variant) } catch { /* unavailable storage */ } }, [resultTab, variant])
  const [srtStyle, setSrtStyle] = useState<TranscriptStyle>('original')
  const wordTimedSrt = !isTranslated && srtStyle !== 'original' && timedStyles?.sourceSrt === sourceSrt
    ? timedStyles.styles?.[srtStyle] : undefined
  const needsWordTiming = engine === 'whisper' && srtStyle !== 'original'
  const srtPreview = useMemo(() => needsWordTiming ? (wordTimedSrt || resultSrt)
    : formatTranscriptSrt(resultSrt, srtStyle, engine), [resultSrt, srtStyle, engine, needsWordTiming, wordTimedSrt])
  const txtPreview = engine === 'capcut' && resultSrt
    ? srtPreviewLines(formatTranscriptSrt(resultSrt, 'original', 'capcut'))
    : resultText
  const selectedOutput = `${isTranslated ? 'translated' : 'source'}-${resultTab}`
  const outputText = resultTab === 'srt' ? srtPreview : txtPreview
  const outputLabel = resultTab === 'srt'
    ? (isTranslated ? t('SRT dịch', 'Translated SRT') : t('SRT nguồn', 'Source SRT'))
    : (isTranslated ? t('TXT dịch', 'Translated TXT') : t('TXT nguồn', 'Source TXT'))
  const styleEnglish = { hard: 'Short cues', v916: 'Portrait 9:16', h169: 'Landscape 16:9', clause: 'Short clauses', sentence: 'Sentences' }
  const styleLabel = srtStyle === 'original' ? t('Giữ cue gốc', 'Original cues')
    : t(SRT_STYLE_OPTIONS.find(option => option.id === srtStyle)!.label, styleEnglish[srtStyle])

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
    <div className="tts-page-panel tts-transcribe-page tts-transcribe-layout">
      <section className="tts-card tts-transcribe-source" id="tts-transcribe">
        <h3 className="tts-card-title">
          <span className="tts-step">1</span> {t('Chép lời', 'Transcribe')}
        </h3>
        <p className="tts-transcribe-description">
          {t(
            'Chọn nguồn nhận dạng giống Clone Video (Whisper / CapCut / OCR / phụ đề), rồi đưa text vào Tạo giọng nói.',
            'Pick a recognition source like Clone Video (Whisper / CapCut / OCR / subtitles), then send text to Create voice.',
          )}
        </p>

        <label className="tts-field tts-transcribe-field tts-source-field">
          <span>{t('Nguồn nhận dạng', 'Recognition source')}</span>
          <select
            value={engine}
            disabled={busy}
            onChange={(e) => {
              onEngineChange(e.target.value as TranscribeEngine)
            }}
          >
            <option value="whisper">{t('Giọng nói (Whisper)', 'Speech (Whisper)')}</option>
            <option value="capcut">{t('Giọng nói (CapCut cloud)', 'Speech (CapCut cloud)')}</option>
            <option value="paddleocr">{t('Chữ trên màn (OCR)', 'On-screen text (OCR)')}</option>
            <option value="subtitle">{t('Phụ đề SRT', 'Subtitle SRT')}</option>
          </select>
          <span className="tts-transcribe-hint">
            {engineHint}
          </span>
        </label>

        <label className="tts-field tts-transcribe-field tts-language-field">
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

        <div className="tts-field tts-transcribe-field tts-translation-toggle-row">
          <span>{t('Có cần dịch không?', 'Do you need translation?')}</span>
          <button
            type="button"
            className={`tts-switch${translation.enabled ? ' is-on' : ''}`}
            role="switch"
            aria-checked={translation.enabled}
            disabled={busy}
            onClick={() => onTranslationChange({ ...translation, enabled: !translation.enabled })}
          >
            <span className="tts-switch-track" aria-hidden="true" />
            <span>{translation.enabled ? t('Có', 'Yes') : t('Không', 'No')}</span>
          </button>
        </div>

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

        {translation.enabled && <>
          <label className="tts-field"><span>{t('Ngôn ngữ đích', 'Target language')}</span><select disabled={busy} value={translation.targetLang} onChange={e => onTranslationChange({ ...translation, targetLang: e.target.value })}>{[['vi', t('Tiếng Việt', 'Vietnamese')], ['en', t('Tiếng Anh', 'English')], ['zh', t('Tiếng Trung', 'Chinese')], ['ja', t('Tiếng Nhật', 'Japanese')], ['ko', t('Tiếng Hàn', 'Korean')]].map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
          <label className="tts-field"><span>{t('API dịch', 'Translation API')}</span><select disabled={busy} value={translation.translator} onChange={e => onTranslationChange({ ...translation, translator: e.target.value, model: '' })}>{Object.keys(PROVIDER_PRESET_MODELS).map(id => <option key={id} value={id}>{id}</option>)}</select></label>
          <label className="tts-field"><span>{t('Model dịch', 'Translation model')}</span><select disabled={busy} required value={translation.model} onChange={e => onTranslationChange({ ...translation, model: e.target.value })}><option value="" disabled>{t('Chọn model dịch', 'Select translation model')}</option>{translation.model && !(PROVIDER_PRESET_MODELS[translation.translator as keyof typeof PROVIDER_PRESET_MODELS] || []).some(model => model.id === translation.model) && <option value={translation.model}>{translation.model}</option>}{(PROVIDER_PRESET_MODELS[translation.translator as keyof typeof PROVIDER_PRESET_MODELS] || []).map(model => <option key={model.id} value={model.id}>{t(model.labelVi, model.labelEn)}</option>)}</select></label>
        </>}
        <div className="tts-transcribe-actions">
          <button
            type="button"
            className="tts-btn tts-btn-blue"
            disabled={busy || !file || (translation.enabled && !translation.model)}
            onClick={() => onSubmit()}
          >
            {busy ? t('Đang nhận dạng…', 'Transcribing…') : t('Nhận dạng', 'Transcribe')}
          </button>
          <button type="button" className="tts-btn tts-btn-ghost" disabled={busy} onClick={onClearCache}>{t('Xóa cache', 'Clear cache')}</button>
        </div>
      </section>

      {sourceText || sourceSrt || (translation.enabled && (translatedText || translatedSrt)) ? (
        <section className="tts-card tts-transcribe-result">
          <h3 className="tts-card-title">
            <span className="tts-step">2</span> {t('Kết quả', 'Result')}
          </h3>
          <div className="tts-result-tabs" role="tablist" aria-label={t('Định dạng kết quả', 'Result format')}>
            <button type="button" role="tab" id="tts-source-txt" aria-controls="tts-result-panel" aria-selected={selectedOutput === 'source-txt'} className={selectedOutput === 'source-txt' ? 'active' : ''} onClick={() => { setVariant('source'); setResultTab('txt') }}>{t('TXT nguồn', 'Source TXT')}</button>
            <button type="button" role="tab" id="tts-source-srt" aria-controls="tts-result-panel" aria-selected={selectedOutput === 'source-srt'} className={selectedOutput === 'source-srt' ? 'active' : ''} onClick={() => { setVariant('source'); setResultTab('srt') }}>{t('SRT nguồn', 'Source SRT')}</button>
            {translation.enabled && translatedText && <>
              <button type="button" role="tab" id="tts-translated-txt" aria-controls="tts-result-panel" aria-selected={selectedOutput === 'translated-txt'} className={selectedOutput === 'translated-txt' ? 'active' : ''} onClick={() => { setVariant('translated'); setResultTab('txt') }}>{t('TXT dịch', 'Translated TXT')}</button>
              <button type="button" role="tab" id="tts-translated-srt" aria-controls="tts-result-panel" aria-selected={selectedOutput === 'translated-srt'} className={selectedOutput === 'translated-srt' ? 'active' : ''} onClick={() => { setVariant('translated'); setResultTab('srt') }}>{t('SRT dịch', 'Translated SRT')}</button>
            </>}
          </div>
          <div id="tts-result-panel" role="tabpanel" aria-labelledby={`tts-${selectedOutput}`} className="tts-result-panel">
          <span className="tts-result-view-label" aria-live="polite">{t('Đang xem:', 'Viewing:')} {outputLabel}{resultTab === 'srt' ? ` · ${styleLabel}` : ''}</span>
          {resultTab === 'srt' && resultSrt && <>
            <select aria-label={t('Kiểu SRT', 'SRT style')} className="tts-srt-style-select" value={srtStyle} onChange={(e) => setSrtStyle(e.target.value as TranscriptStyle)}><option value="original">{t('Giữ cue gốc', 'Original cues')}</option>{SRT_STYLE_OPTIONS.map((option) => <option key={option.id} value={option.id}>{t(option.label, styleEnglish[option.id])}</option>)}</select>
            <span className="tts-transcribe-hint">{srtStyle === 'original' && engine === 'capcut'
              ? t('Giữ nguyên từng cue và timecode CapCut cloud; TXT cũng lấy đúng các cue này.', 'Keeps every CapCut cloud cue and timestamp; TXT uses the same cue boundaries.')
              : srtStyle === 'original'
                ? t('Giữ nguyên nội dung và timecode nguồn.', 'Keeps source content and timecodes unchanged.')
              : needsWordTiming
                ? wordTimedSrt
                  ? t('Chia cue và timecode bằng timestamp từng từ của Whisper.', 'Splits cues and timecodes using Whisper word timestamps.')
                  : isTranslated
                    ? t('Bản dịch chưa có timestamp từng từ; giữ cue và timecode bản dịch gốc.', 'Translation has no word timestamps; keeping its original cues and timecodes.')
                    : t('Chưa có timestamp từng từ khớp kết quả này cho kiểu đã chọn. Đang giữ SRT gốc; nhận dạng lại để lấy dữ liệu thời gian.', 'No matching word timestamps for this result and style. Keeping original SRT; transcribe again to obtain timing data.')
                : t('Chỉ ghép tại mốc cue nguồn; không đoán thời gian bên trong cue.', 'Groups only at source cue boundaries; never estimates timing within a cue.')}</span>
            {srtStyle !== 'original' && (!needsWordTiming || wordTimedSrt) && srtPreview === resultSrt && <span className="tts-transcribe-hint" role="status">{t('Kiểu này cho cùng kết quả: cue hiện tại không cần chia hoặc ghép thêm.', 'This style produces the same result: current cues need no additional splitting or grouping.')}</span>}
          </>}
          <textarea
            key={selectedOutput}
            aria-label={outputLabel}
            className={`tts-textarea${resultTab === 'srt' ? ' tts-result-srt' : ''}`}
            rows={12}
            value={outputText}
            placeholder={resultTab === 'srt' ? t('Chưa có SRT cho kết quả này. Bấm Nhận dạng để tạo SRT có timecode nguồn.', 'No SRT for this result yet. Click Transcribe to generate SRT with source timecodes.') : ''}
            readOnly={resultTab === 'srt' && (srtStyle !== 'original' || !resultSrt)}
            disabled={busy}
            onChange={(e) => (resultTab === 'srt' ? onSrtChange : onResultChange)(e.target.value)}
          />
          </div>
          <div className="tts-transcribe-actions">
            <button type="button" className="tts-btn tts-btn-blue" disabled={busy || !outputText.trim()} onClick={() => onApplyToTts(resultTab, outputText)}>
              {t('Đưa vào Tạo giọng nói', 'Send to Create voice')}
            </button>
            {resultTab === 'srt' && resultSrt ? <button type="button" className="tts-btn" disabled={busy} onClick={() => { const blob = new Blob([srtPreview], { type: 'application/x-subrip;charset=utf-8' }); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = `transcript-${isTranslated ? 'translated' : 'source'}-${srtStyle}.srt`; a.click(); URL.revokeObjectURL(url) }}>{t('Tải SRT cho CapCut', 'Download SRT for CapCut')}</button> : null}
            <button type="button" className="tts-btn tts-btn-primary" disabled={busy || !outputText.trim()} onClick={() => onRewrite(outputText)}>{t('AI viết lại', 'Rewrite with AI')}</button>
          </div>
        </section>
      ) : null}
    </div>
  )
}
