/** Panel «Nhập nội dung» của dashboard TTS: tab văn bản / SRT / TXT / clipboard. */
import { useRef, type Dispatch, type SetStateAction } from 'react'
import { localize, useLocale } from '@/app/i18n'
import { IconFile, IconList, IconPaste } from './TtsIcons'

type Props = {
  inputMode: 'text' | 'srt'
  text: string
  srtRaw: string
  keepTimeline: boolean
  autoSplit: boolean
  setAutoSplit: Dispatch<SetStateAction<boolean>>
  onSwitchMode: (mode: 'text' | 'srt') => void
  /** File .txt đã chọn — caller reset mode text + xóa srtRaw rồi đọc file. */
  onPickTxt: (file: File) => void
  onPickSrt: (file: File) => void
  onTextChange: (value: string) => void
  onSrtChange: (value: string) => void
  onClearText: () => void
  onClearSrt: () => void
  onPasteClipboard: () => void
  /** VieNeu Turbo: hiện gợi ý cue phi ngôn ngữ [cười] [thở dài] [hắng giọng]. */
  emotionCues?: boolean
}

// Tags understood by the VieNeu SDK phonemizer (vieneu_utils.phonemize_text).
const EMOTION_CUES: [string, string][] = [
  ['[cười]', '[chuckle]'],
  ['[thở dài]', '[sigh]'],
  ['[hắng giọng]', '[clear throat]'],
]

export default function TtsInputPanel({
  inputMode,
  text,
  srtRaw,
  keepTimeline,
  autoSplit,
  setAutoSplit,
  onSwitchMode,
  onPickTxt,
  onPickSrt,
  onTextChange,
  onSrtChange,
  onClearText,
  onClearSrt,
  onPasteClipboard,
  emotionCues,
}: Props) {
  const { locale } = useLocale()
  const t = (vietnamese: string, english: string) => localize(locale, vietnamese, english)
  const textRef = useRef<HTMLTextAreaElement>(null)
  const insertCue = (cue: string) => {
    const el = textRef.current
    const start = el?.selectionStart ?? text.length
    const end = el?.selectionEnd ?? text.length
    const pad = start > 0 && !/\s$/.test(text.slice(0, start)) ? ' ' : ''
    const inserted = `${pad}${cue} `
    onTextChange(text.slice(0, start) + inserted + text.slice(end))
    const caret = start + inserted.length
    requestAnimationFrame(() => {
      el?.focus()
      el?.setSelectionRange(caret, caret)
    })
  }
  const fileRef = useRef<HTMLInputElement>(null)
  const srtRef = useRef<HTMLInputElement>(null)
  return (
    <section className="tts-card" id="tts-input">
      <h3 className="tts-card-title"><span className="tts-step">1</span> Nhập nội dung</h3>
      <div className="tts-tabs">
        <button
          type="button"
          className={inputMode === 'text' ? 'active' : undefined}
          onClick={() => onSwitchMode('text')}
        >
          <IconFile size={12} /> Nhập văn bản
        </button>
        <button
          type="button"
          className={inputMode === 'srt' ? 'active' : undefined}
          title="Mở mode SRT — chọn file hoặc dán nội dung .srt"
          onClick={() => {
            onSwitchMode('srt')
            if (!srtRaw.trim()) srtRef.current?.click()
          }}
        >
          <IconList size={12} /> Nhập SRT
        </button>
        <button type="button" onClick={() => fileRef.current?.click()}>
          <IconFile size={12} /> Nhập TXT
        </button>
        <button type="button" onClick={onPasteClipboard}>
          <IconPaste size={12} /> Dán clipboard
        </button>
      </div>
      <input
        ref={fileRef}
        type="file"
        accept=".txt,text/plain"
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) onPickTxt(f)
          e.target.value = ''
        }}
      />
      <input
        ref={srtRef}
        type="file"
        accept=".srt,text/plain,.vtt"
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) onPickSrt(f)
          e.target.value = ''
        }}
      />
      {inputMode === 'srt' ? (
        <>
          <textarea
            className="tts-textarea"
            value={srtRaw}
            onChange={(e) => onSrtChange(e.target.value)}
            spellCheck={false}
            placeholder={
              '1\n00:00:01,000 --> 00:00:04,000\nXin chào…\n\n(dán SRT đầy đủ — 1 cue = 1 câu TTS, giữ timestamp)'
            }
          />
          <div className="tts-foot-row">
            <span>
              {srtRaw.length} ký tự
              {srtRaw.trim() ? ' · mode SRT' : ''}
              {keepTimeline ? ' · giữ timeline' : ''}
            </span>
            <button type="button" onClick={onClearSrt}>
              Xóa nội dung
            </button>
          </div>
          <p style={{ margin: '6px 0 0', fontSize: '0.72rem', color: 'var(--tts-muted)' }}>
            SRT: 1 cue = 1 dòng TTS; xuất phụ đề giữ start/end gốc khi bật « Giữ nguyên timeline SRT ».
            Không tách câu CapCut.
          </p>
        </>
      ) : (
        <>
          <textarea
            ref={textRef}
            className="tts-textarea"
            value={text}
            onChange={(e) => onTextChange(e.target.value)}
            placeholder="Nhập hoặc dán văn bản của bạn ở đây…"
          />
          {emotionCues && (
            <div id="tts-emotion-cues" className="tts-cues">
              <span>{t('Cảm xúc / cue phi ngôn ngữ: chèn thẳng vào văn bản:', 'Emotion / non-verbal cues: drop straight into the text:')}</span>
              <div className="tts-cues-row">
                {EMOTION_CUES.map(([vi, en], i) => (
                  <button key={vi} type="button" className="tts-cue" data-cue={i + 1} onClick={() => insertCue(locale === 'en' ? en : vi)}>
                    {locale === 'en' ? en : vi}
                  </button>
                ))}
              </div>
            </div>
          )}
          <div className="tts-foot-row">
            <span>{text.length} ký tự</span>
            <button type="button" onClick={onClearText}>
              Xóa nội dung
            </button>
          </div>
          <div className="tts-split-row">
            <label className="tts-split-field">
              Tùy chọn tách câu
              <select
                value={autoSplit ? 'auto' : 'off'}
                onChange={(e) => setAutoSplit(e.target.value === 'auto')}
              >
                <option value="auto">Tự động tách câu (khuyến nghị)</option>
                <option value="off">Không tách</option>
              </select>
            </label>
            <button
              type="button"
              className={autoSplit ? 'tts-switch is-on' : 'tts-switch'}
              role="switch"
              aria-checked={autoSplit}
              title={autoSplit ? 'Tắt tách câu' : 'Bật tách câu'}
              onClick={() => setAutoSplit((v) => !v)}
            >
              <span className="tts-switch-track" />
            </button>
          </div>
          <p style={{ margin: '6px 0 0', fontSize: '0.72rem', color: 'var(--tts-muted)' }}>
            {t(
              'Dòng trống luôn tách đoạn. “Tự động tách câu” thêm bước cắt theo . ! ?',
              'Blank lines always split paragraphs. “Auto-split sentences” also cuts on . ! ?',
            )}
          </p>
        </>
      )}
    </section>
  )
}
