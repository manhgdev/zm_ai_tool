import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { fetchJson } from '@/shared/api/fetchJson'
import { SRT_STYLE_OPTIONS } from '@/features/tts/lib/srt'
import { IconDownload } from '@/shared/components/Icons'
import { translatorOptions, normalizeTranslatorForEngine, loadSettings } from '@/app/appSettings'
import { localize, useLocale } from '@/app/i18n'
import { BackTitle } from '@/shared/components/BackTitle'
import { OutputFolderField } from '@/shared/components/OutputFolderField'
import './SrtExportPage.css'

type SourceKind = 'media' | 'caption' | 'manual' | 'url'
type OutputMode = 'original' | 'translated' | 'bilingual'
type RecognitionEngine = 'whisper' | 'capcut'
type Job = { id: string; filename: string; sourceKind: SourceKind | 'platform'; status: 'queued' | 'processing' | 'done' | 'error' | 'cancelled'; progress: number; message: string; error?: string; files: string[]; options?: { outputMode?: OutputMode; targetLang?: string; recognitionEngine?: RecognitionEngine } }
const CACHE_KEY = 'zm_ai_tool.srt-export.source-kind'
const JOB_KEY = 'zm_ai_tool.srt-export.job-id.v1'
const OUTPUT_DIR_KEY = 'zm_ai_tool.srt-export.output-dir.v1'
const LANGUAGE_LABELS: Record<string, string> = { vi: 'Tiếng Việt', en: 'Tiếng Anh', zh: 'Tiếng Trung', ja: 'Tiếng Nhật', ko: 'Tiếng Hàn' }

function loadKind(): SourceKind {
  try { const value = localStorage.getItem(CACHE_KEY); return value === 'caption' || value === 'manual' || value === 'url' ? value : 'media' } catch { return 'media' }
}

export default function SrtExportPage({ onBack }: { onBack: () => void }) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const inputRef = useRef<HTMLInputElement>(null)
  const [kind, setKind] = useState<SourceKind>(loadKind)
  const [file, setFile] = useState<File | null>(null)
  const [manualText, setManualText] = useState('')
  const [sourceUrl, setSourceUrl] = useState('')
  const [job, setJob] = useState<Job | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [outputDir, setOutputDir] = useState(() => localStorage.getItem(OUTPUT_DIR_KEY) || '')
  const [isDesktopApp, setIsDesktopApp] = useState(false)
  const saved = useRef(loadSettings()).current
  const [outputMode, setOutputMode] = useState<OutputMode>('original')
  const [sourceLang, setSourceLang] = useState(saved.sourceLang)
  const [targetLang, setTargetLang] = useState(saved.targetLang === 'none' ? 'vi' : saved.targetLang)
  const [translator, setTranslator] = useState(() => normalizeTranslatorForEngine('whisper', saved.translator))
  const [recognitionEngine, setRecognitionEngine] = useState<RecognitionEngine>('whisper')
  const capcutTranslate = kind === 'media' && recognitionEngine === 'capcut'

  useEffect(() => { try { localStorage.setItem(CACHE_KEY, kind) } catch {} }, [kind])
  useEffect(() => { try { localStorage.setItem(OUTPUT_DIR_KEY, outputDir) } catch {} }, [outputDir])
  useEffect(() => { void fetchJson<{ desktop?: boolean }>('/api/config').then((config) => setIsDesktopApp(Boolean(config.desktop))).catch(() => undefined) }, [])
  // Source files are moved into the job workspace on submit.  Re-open the
  // most recent workspace after F5 so processing/downloads do not disappear
  // from the UI while the backend job still exists.
  useEffect(() => {
    let cancelled = false
    void fetchJson<Job[]>('/api/srt-export/jobs').then((jobs) => {
      if (cancelled || !jobs.length) return
      let cachedId = ''
      try { cachedId = localStorage.getItem(JOB_KEY) || '' } catch { /* unavailable storage */ }
      setJob(jobs.find((item) => item.id === cachedId) ?? jobs[0])
    }).catch(() => undefined)
    return () => { cancelled = true }
  }, [])
  useEffect(() => {
    try {
      if (job?.id) localStorage.setItem(JOB_KEY, job.id)
      else localStorage.removeItem(JOB_KEY)
    } catch { /* unavailable storage */ }
  }, [job?.id])
  useEffect(() => {
    if (!job || !['queued', 'processing'].includes(job.status)) return
    const timer = window.setInterval(() => fetchJson<Job>(`/api/srt-export/jobs/${job.id}`).then(setJob).catch(() => {}), 900)
    return () => window.clearInterval(timer)
  }, [job?.id, job?.status])

  async function submit() {
    if ((!file && kind !== 'manual' && kind !== 'url') || (kind === 'manual' && !manualText.trim()) || (kind === 'url' && !sourceUrl.trim()) || busy) return
    setBusy(true); setError('')
    try {
      const form = new FormData()
      if (file) form.append('file', file)
      form.append('source_kind', kind)
      form.append('manual_text', manualText)
      form.append('source_url', sourceUrl)
      form.append('output_mode', outputMode)
      form.append('source_lang', sourceLang)
      form.append('target_lang', targetLang)
      form.append('translator', translator)
      form.append('recognition_engine', recognitionEngine)
      form.append('workers', String(saved.workers))
      form.append('ollama_mode', saved.ollamaMode)
      form.append('ollama_model', saved.ollamaModel)
      form.append('ollama_local_tier', saved.ollamaLocalTier)
      form.append('output_dir', outputDir)
      const res = await fetchJson<Job>('/api/srt-export/jobs', { method: 'POST', body: form }, 60_000)
      setJob(res)
      toast.success(t('Đã bắt đầu tạo phụ đề.', 'Started creating subtitles.'))
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Không thể tạo phụ đề'
      setError(msg)
      toast.error(msg)
    }
    finally { setBusy(false) }
  }

  async function cancel() {
    if (!job) return
    await fetchJson(`/api/srt-export/jobs/${job.id}/cancel`, { method: 'POST' })
    setJob({ ...job, status: 'cancelled', message: 'Đã hủy' })
    toast.success(t('Đã hủy tạo phụ đề.', 'Subtitle generation cancelled.'))
  }

  const accepted = kind === 'media'
    ? '.mp3,.wav,.m4a,.aac,.flac,.ogg,.mp4,.mov,.mkv,.webm,.avi,.m4v'
    : '.srt,.vtt,.txt'
  const sourceFiles = job?.files.filter((name) => name.startsWith('subtitles-source-')) || []
  const zipFiles = job?.files.filter((name) => name.endsWith('.zip')) || []
  const translatedFiles = job?.files.filter((name) => !name.startsWith('subtitles-source-') && !name.endsWith('.zip')) || []
  const isBilingualResult = job?.options?.outputMode === 'bilingual' || sourceFiles.length > 0
  const targetLabel = LANGUAGE_LABELS[job?.options?.targetLang || targetLang] || (job?.options?.targetLang || targetLang).toUpperCase()

  function selectRecognitionEngine(value: RecognitionEngine) {
    setRecognitionEngine(value)
    if (value === 'capcut' && outputMode === 'original') setOutputMode('translated')
  }
  async function pickOutputDir() {
    const picked = await fetchJson<{ path?: string }>('/api/system/pick-folder', { method: 'POST' }, 300_000)
    return picked.path || undefined
  }

  return <main className="srt-export-page">
    <header className="srt-export-head">
      <BackTitle onBack={onBack}>{t('Xuất Phụ Đề', 'Export subtitles')}</BackTitle>
      <p>{t('Tạo phụ đề từ audio/video hoặc định dạng lại caption có sẵn để dùng trong CapCut, YouTube và các trình dựng video.', 'Create subtitles from audio/video or reformat existing captions for CapCut, YouTube, and video editors.')}</p>
    </header>
    <section className="srt-export-card">
      <div className="srt-export-tabs" role="tablist" aria-label={t('Nguồn phụ đề', 'Subtitle source')}>
        <button className={kind === 'media' ? 'active' : undefined} onClick={() => { setKind('media'); setFile(null) }}>{t('Từ audio / video', 'From audio / video')}</button>
        <button className={kind === 'caption' ? 'active' : undefined} onClick={() => { setKind('caption'); setFile(null) }}>{t('Từ SRT / caption / file', 'From SRT / caption / file')}</button>
        <button className={kind === 'manual' ? 'active' : undefined} onClick={() => { setKind('manual'); setFile(null) }}>{t('Nhập bằng tay', 'Enter manually')}</button>
        <button className={kind === 'url' ? 'active' : undefined} onClick={() => { setKind('url'); setFile(null) }}>{t('Từ URL', 'From URL')}</button>
      </div>
      <div className="srt-export-body">
        <h2>{kind === 'media' ? t('Chọn audio hoặc video', 'Choose audio or video') : kind === 'caption' ? t('Chọn file phụ đề có sẵn', 'Choose an existing subtitle file') : kind === 'manual' ? t('Nhập nội dung caption', 'Enter caption content') : t('Dán URL từ nền tảng hoặc file trực tiếp', 'Paste a platform or direct-file URL')}</h2>
        <p>{kind === 'media' ? capcutTranslate ? t('CapCut nhận dạng và dịch trực tiếp trên cloud, rồi trả SRT có timecode. Không chạy Whisper hoặc API dịch khác.', 'CapCut recognizes and translates in the cloud, then returns a timed SRT. Whisper and other translation APIs are not used.') : t('Whisper tự nhận dạng lời nói và giữ mốc thời gian.', 'Whisper recognizes speech and preserves timestamps.') : kind === 'manual' ? t('Mỗi dòng là một caption. Với nội dung có timecode, hãy dùng định dạng SRT.', 'Each line is one caption. Use SRT when the content has timecodes.') : kind === 'url' ? t('Ưu tiên subtitle/caption sẵn có trên nền tảng. Khi không có, app mới tải audio và dùng Whisper.', 'Prefer subtitles/captions available on the platform. Only when absent will the app download audio and use Whisper.') : t('Hỗ trợ SRT, VTT và TXT. SRT/VTT giữ timecode; TXT chia mỗi dòng thành một caption ngắn.', 'Supports SRT, VTT, and TXT. SRT/VTT preserves timecodes; TXT makes each line a short caption.')}</p>
        {kind === 'manual' ? <textarea className="srt-export-textarea" value={manualText} onChange={(event) => setManualText(event.target.value)} placeholder={t('Nhập từng câu phụ đề, mỗi dòng một caption…', 'Enter one subtitle sentence per line…')} rows={7} /> : kind === 'url' ? <input className="srt-export-url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="https://www.tiktok.com/... hoặc https://youtube.com/..." /> : <><input ref={inputRef} type="file" accept={accepted} hidden onChange={(event) => { const f = event.target.files?.[0] || null; setFile(f); if (f) toast.success(t('Đã chọn file nguồn.', 'Source file selected.')) }} /><button className="srt-export-picker" type="button" onClick={() => inputRef.current?.click()}>{file ? file.name : t('Chọn file', 'Choose file')}</button>{file && <span className="srt-export-file">{(file.size / 1024 / 1024).toFixed(file.size > 10 * 1024 * 1024 ? 0 : 1)} MB</span>}</>}
        <OutputFolderField isDesktopApp={isDesktopApp} value={outputDir} onChange={setOutputDir} onChoose={isDesktopApp ? () => pickOutputDir().catch((e) => setError(e instanceof Error ? e.message : String(e))) : undefined} defaultPath={t('Ví dụ: du-an-01 hoặc phu-de.srt', 'Example: project-01 or subtitles.srt')} appFolder="subtitles/export" />
      </div>
      <div className="srt-export-settings">
        {kind === 'media' && <label><span>{t('Nhận dạng & dịch', 'Recognition & translation')}</span><select value={recognitionEngine} onChange={(event) => selectRecognitionEngine(event.target.value as RecognitionEngine)}><option value="whisper">{t('Whisper + công cụ dịch', 'Whisper + translation provider')}</option><option value="capcut">{t('CapCut dịch (không dùng Whisper)', 'CapCut Translate (no Whisper)')}</option></select></label>}
        <label><span>{t('Kiểu phụ đề', 'Subtitle output')}</span><select value={outputMode} onChange={(event) => setOutputMode(event.target.value as OutputMode)}>{!capcutTranslate && <option value="original">{t('Bản gốc', 'Original')}</option>}<option value="translated">{t('Bản dịch', 'Translation')}</option><option value="bilingual">{t('Song ngữ (2 bộ file: gốc và dịch)', 'Bilingual (two sets: source and translation)')}</option></select></label>
        <label><span>{t('Ngôn ngữ gốc', 'Source language')}</span><select value={sourceLang} onChange={(event) => setSourceLang(event.target.value)}><option value="auto">{t('Tự động nhận diện', 'Auto detect')}</option><option value="vi">{t('Tiếng Việt', 'Vietnamese')}</option><option value="en">{t('Tiếng Anh', 'English')}</option><option value="zh">{t('Tiếng Trung', 'Chinese')}</option><option value="ja">{t('Tiếng Nhật', 'Japanese')}</option><option value="ko">{t('Tiếng Hàn', 'Korean')}</option></select></label>
        {outputMode !== 'original' && <><label><span>{t('Ngôn ngữ dịch', 'Translate to')}</span><select value={targetLang} onChange={(event) => setTargetLang(event.target.value)}><option value="vi">{t('Tiếng Việt', 'Vietnamese')}</option><option value="en">{t('Tiếng Anh', 'English')}</option><option value="zh">{t('Tiếng Trung', 'Chinese')}</option><option value="ja">{t('Tiếng Nhật', 'Japanese')}</option><option value="ko">{t('Tiếng Hàn', 'Korean')}</option></select></label>{!capcutTranslate && <label><span>{t('Công cụ dịch', 'Translation provider')}</span><select value={translator} onChange={(event) => setTranslator(event.target.value as typeof translator)}>{translatorOptions('whisper').map(({ id, label }) => <option key={id} value={id}>{label}</option>)}</select></label>}</>}
      </div>
      <div className="srt-export-outputs">
        <strong>{t('File xuất tự động', 'Automatic output files')}</strong>
        <div className="srt-export-chips">
          {SRT_STYLE_OPTIONS.map((style) => <span key={style.id}>{style.label}</span>)}
          <span>WebVTT</span><span>TXT</span><span>{t('ZIP (tất cả)', 'ZIP (all)')}</span>
        </div>
      </div>
      {error && <p className="srt-export-error">{error}</p>}
      <footer className="srt-export-actions">
        <button className="srt-export-run" disabled={(!file && kind !== 'manual' && kind !== 'url') || (kind === 'manual' && !manualText.trim()) || (kind === 'url' && !sourceUrl.trim()) || busy || job?.status === 'processing'} onClick={submit}>{busy ? t('Đang gửi nguồn…', 'Sending source…') : kind === 'media' ? capcutTranslate ? t('CapCut dịch & tải SRT', 'Translate with CapCut & download SRT') : t('Tạo phụ đề', 'Create subtitles') : t('Xuất phụ đề', 'Export subtitles')}</button>
        {job && ['queued', 'processing'].includes(job.status) && <button className="srt-export-cancel" onClick={cancel}>{t('Hủy', 'Cancel')}</button>}
      </footer>
    </section>
    {job && <section className="srt-export-card srt-export-result">
      <div className="srt-export-result-head"><strong>{job.filename}</strong><span className={`srt-export-status ${job.status}`}>{job.message}</span></div>
      <div className="srt-export-progress"><i style={{ width: `${job.progress}%` }} /></div>
      {job.error && <p className="srt-export-error">{job.error}</p>}
      {job.status === 'done' && (isBilingualResult ? <div className="srt-export-download-groups">
        <section><h2>Phụ đề gốc</h2><div className="srt-export-downloads">{sourceFiles.map((name) => <a key={name} href={`/api/srt-export/jobs/${job.id}/files/${encodeURIComponent(name)}`} download><IconDownload size={15} /><span>{name}</span><small>Tải về</small></a>)}</div></section>
        <section><h2>Bản dịch · {targetLabel}</h2><div className="srt-export-downloads">{translatedFiles.map((name) => <a key={name} href={`/api/srt-export/jobs/${job.id}/files/${encodeURIComponent(name)}`} download><IconDownload size={15} /><span>{name}</span><small>Tải về</small></a>)}</div></section>
        <div className="srt-export-downloads">{zipFiles.map((name) => <a key={name} className="primary" href={`/api/srt-export/jobs/${job.id}/files/${encodeURIComponent(name)}`} download><IconDownload size={15} /><span>{name}</span><small>Tải tất cả</small></a>)}</div>
      </div> : <div className="srt-export-downloads">
        {job.files.map((name) => <a key={name} className={name.endsWith('.zip') ? 'primary' : undefined} href={`/api/srt-export/jobs/${job.id}/files/${encodeURIComponent(name)}`} download><IconDownload size={15} /><span>{name}</span><small>Tải về</small></a>)}
      </div>)}
    </section>}
  </main>
}
