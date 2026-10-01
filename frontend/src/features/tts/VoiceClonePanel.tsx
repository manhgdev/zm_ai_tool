/** Form «Clone giọng nói» — dùng ở trang Clone (variant `page`) và dashboard (variant `dash`). */
import { useState, type DragEvent } from 'react'
import { VoiceTagPicker, type VoiceTagLabel } from './VoiceMetadataModal'
import { IconUpload } from './TtsIcons'

type Props = {
  variant: 'page' | 'dash'
  cloneName: string
  cloneFile: File | null
  cloneTags: VoiceTagLabel[]
  cloneCount: number
  busy: boolean
  onNameChange: (name: string) => void
  onFileChange: (file: File | null) => void
  onTagsChange: (tags: VoiceTagLabel[]) => void
  onSubmit: () => void
  /** Mở tab «Danh sách giọng» */
  onVoiceList: () => void
}

export default function VoiceClonePanel({
  variant,
  cloneName,
  cloneFile,
  cloneTags,
  cloneCount,
  busy,
  onNameChange,
  onFileChange,
  onTagsChange,
  onSubmit,
  onVoiceList,
}: Props) {
  const [isDragging, setIsDragging] = useState(false)
  const fileInputId = variant === 'dash' ? 'tts-clone-file-dash' : 'tts-clone-file'
  const introStyle =
    variant === 'dash'
      ? { margin: '0 0 12px', fontSize: '0.8rem', color: 'var(--tts-muted)', lineHeight: 1.6, letterSpacing: '0.02em' }
      : { margin: '0 0 10px', fontSize: '0.78rem', color: 'var(--tts-muted)' }
  return (
    <section className="tts-card" id="tts-clone">
      <h3 className="tts-card-title">
        <span className="tts-step">4</span> Clone giọng nói (TTS)
        <span className="tts-badge-new">Mới</span>
      </h3>
      <p style={introStyle}>
        Tạo giọng nói tùy chỉnh từ giọng của bạn
        {cloneCount > 0 ? (
          <>
            {' · '}
            <button
              type="button"
              className="link"
              style={{ background: 'none', border: 0, cursor: 'pointer', padding: 0, font: 'inherit', color: 'inherit' }}
              onClick={onVoiceList}
            >
              {cloneCount} giọng đã lưu{variant === 'dash' ? ' — quản lý' : ''}
            </button>
          </>
        ) : null}
      </p>
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
          const file = event.dataTransfer.files?.[0]
          if (file) onFileChange(file)
        }}
      >
        <div className="ico"><IconUpload size={20} /></div>
        <p>Kéo & thả file audio vào đây<br />hoặc</p>
        <button
          type="button"
          className="tts-btn tts-btn-ghost"
          onClick={() => document.getElementById(fileInputId)?.click()}
        >
          Chọn file audio
        </button>
        <input
          id={fileInputId}
          type="file"
          accept="audio/*,.wav,.mp3,.m4a"
          hidden
          onChange={(e) => onFileChange(e.target.files?.[0] || null)}
        />
        <div className="hint">
          {cloneFile ? cloneFile.name : 'Định dạng hỗ trợ: WAV, MP3, M4A · Tối thiểu 10 giây, tối đa 5 phút'}
        </div>
      </div>
      <label className="tts-field">
        <span>Tên giọng</span>
        <input type="text" value={cloneName} placeholder="Ví dụ: Giọng của tôi" onChange={(e) => onNameChange(e.target.value)} />
      </label>
      <VoiceTagPicker value={cloneTags} onChange={onTagsChange} />
      <button
        type="button"
        className="tts-btn tts-btn-primary tts-btn-block"
        disabled={busy || !cloneFile || !cloneName.trim()}
        onClick={onSubmit}
      >
        Tạo giọng clone
      </button>
    </section>
  )
}
