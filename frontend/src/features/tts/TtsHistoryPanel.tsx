/** Bảng «Lịch sử tạo giọng»: phân trang + menu tải WAV/MP3/SRT/ZIP. */
import { useEffect, useMemo, type Dispatch, type SetStateAction } from 'react'
import { localize, useLocale } from '@/app/i18n'
import type { HistoryItem, Voice } from './tts.types'
import { engineLabel, voiceDisplayName } from './lib/voiceDisplay'
import { HISTORY_MAX, HISTORY_PAGE_SIZE, fmtDur } from './lib/format'
import { SRT_STYLE_OPTIONS } from './lib/srt'
import { historyDownloadUrl } from './lib/download'
import { IconDownload, IconFile, IconPlay, IconStop, IconTrash } from './TtsIcons'

type Props = {
  history: HistoryItem[]
  voices: Voice[]
  page: number
  setPage: Dispatch<SetStateAction<number>>
  /** Menu state ở TtsStudio — dùng chung listener đóng menu với menu SRT chính. */
  downloadMenuId: string | null
  historySrtMenuId: string | null
  onToggleDownloadMenu: (id: string) => void
  onToggleSrtMenu: (id: string) => void
  playingHistoryId: string | null
  onPlay: (h: HistoryItem) => void
  onDelete: (h: HistoryItem) => void
  onDeleteAll: () => void
  isDesktopApp?: boolean
  /** APP mở file local trong Finder/Explorer; WEB mới tải qua trình duyệt. */
  onReveal: (jobId: string, kind: 'wav' | 'mp3' | 'srt' | 'zip', style?: string) => void
  /** Tải file + đóng mọi menu (triggerDownload của TtsStudio). */
  onDownload: (url: string | undefined, filename: string) => void
}

export default function TtsHistoryPanel({
  history,
  voices,
  page,
  setPage,
  downloadMenuId,
  historySrtMenuId,
  onToggleDownloadMenu,
  onToggleSrtMenu,
  playingHistoryId,
  onPlay,
  onDelete,
  onDeleteAll,
  isDesktopApp = false,
  onReveal,
  onDownload,
}: Props) {
  const { locale } = useLocale()
  const t = (vietnamese: string, english: string) => localize(locale, vietnamese, english)
  const historyCapped = useMemo(() => history.slice(0, HISTORY_MAX), [history])
  const totalPages = Math.max(1, Math.ceil(historyCapped.length / HISTORY_PAGE_SIZE) || 1)
  const pageSafe = Math.min(Math.max(1, page), totalPages)
  const pageItems = useMemo(() => {
    const start = (pageSafe - 1) * HISTORY_PAGE_SIZE
    return historyCapped.slice(start, start + HISTORY_PAGE_SIZE)
  }, [historyCapped, pageSafe])
  const offset = (pageSafe - 1) * HISTORY_PAGE_SIZE

  useEffect(() => {
    if (page > totalPages) setPage(totalPages)
  }, [page, totalPages, setPage])

  return (
    <>
      <div className="tts-history-toolbar">
        <strong>{t('Các bản ghi đã tạo', 'Generated recordings')}</strong>
        <button type="button" className="tts-history-clear" disabled={!historyCapped.length} onClick={onDeleteAll}>
          <IconTrash size={12} /> {t('Xóa tất cả', 'Delete all')}
        </button>
      </div>
      <div className="tts-history-wrap">
        <table className="tts-history">
          <thead>
            <tr>
              <th>#</th>
              <th>Tiêu đề / tên</th>
              <th>Engine</th>
              <th>Giọng</th>
              <th>Thời lượng</th>
              <th>Ngày tạo</th>
              <th>File audio</th>
              <th>File SRT</th>
              <th>Trạng thái</th>
              <th>Hành động</th>
            </tr>
          </thead>
          <tbody>
            {pageItems.length === 0 && (
              <tr>
                <td colSpan={10} className="tts-empty">Chưa có lịch sử — tạo giọng nói để bắt đầu</td>
              </tr>
            )}
            {pageItems.map((h, i) => {
              const isPlaying = playingHistoryId === h.id
              return (
              <tr key={h.id}>
                <td>{offset + i + 1}</td>
                <td style={{ fontWeight: 600 }}>{h.title || h.id}</td>
                <td>{engineLabel(h.engine, h.voice)}</td>
                <td
                  style={{ color: 'var(--tts-muted)', maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis' }}
                  title={voiceDisplayName(h.voice, voices, h.voiceName)}
                >
                  {voiceDisplayName(h.voice, voices, h.voiceName)}
                </td>
                <td>{fmtDur(h.duration)}</td>
                <td style={{ color: 'var(--tts-muted)', whiteSpace: 'nowrap' }}>{h.createdAt || '—'}</td>
                <td>
                  {h.audioUrl ? (
                    <button
                      type="button"
                      className="link"
                      style={{ background: 'none', border: 0, cursor: 'pointer', padding: 0, font: 'inherit' }}
                      onClick={() => onPlay(h)}
                    >
                      {(h.title || h.id).slice(0, 16)}.wav
                    </button>
                  ) : '—'}
                </td>
                <td style={{ color: 'var(--tts-muted)' }}>—</td>
                <td className="tts-tag-ok">Hoàn thành</td>
                <td>
                  <div className="tts-act" data-dl-menu>
                    {h.audioUrl && (
                      <button
                        type="button"
                        className={isPlaying ? 'is-playing' : undefined}
                        title={isPlaying ? t('Dừng phát', 'Stop playback') : t('Nghe', 'Play')}
                        aria-label={isPlaying ? t('Dừng phát', 'Stop playback') : t('Nghe', 'Play')}
                        onClick={() => onPlay(h)}
                      >
                        {isPlaying ? <IconStop size={12} /> : <IconPlay size={12} />}
                      </button>
                    )}
                    {h.audioUrl && (
                      <div className={`tts-dl-wrap${downloadMenuId === h.id ? ' is-open' : ''}`}>
                        <button
                          type="button"
                          title={isDesktopApp
                            ? t('Mở kết quả — chọn định dạng', 'Open output — choose format')
                            : t('Tải xuống — chọn định dạng', 'Download — choose format')}
                          className={downloadMenuId === h.id ? 'is-open' : undefined}
                          onClick={(e) => {
                            e.stopPropagation()
                            onToggleDownloadMenu(h.id)
                          }}
                        >
                          {isDesktopApp ? <IconFile size={12} /> : <IconDownload size={12} />}
                        </button>
                        {downloadMenuId === h.id && (
                          <div className="tts-dl-menu tts-history-dl-menu" role="menu">
                            <button
                              type="button"
                              role="menuitem"
                              onClick={() => isDesktopApp
                                ? onReveal(h.id, 'wav')
                                : onDownload(
                                    historyDownloadUrl(h, 'wav'),
                                    `${(h.title || h.id).slice(0, 40)}.wav`,
                                  )}
                            >
                              WAV
                            </button>
                            <button
                              type="button"
                              role="menuitem"
                              onClick={() => isDesktopApp
                                ? onReveal(h.id, 'mp3')
                                : onDownload(
                                    historyDownloadUrl(h, 'mp3'),
                                    `${(h.title || h.id).slice(0, 40)}.mp3`,
                                  )}
                            >
                              MP3
                            </button>
                            <div className="tts-dl-subwrap">
                              <button
                                type="button"
                                role="menuitem"
                                aria-haspopup="menu"
                                aria-expanded={historySrtMenuId === h.id}
                                onClick={(e) => {
                                  e.stopPropagation()
                                  onToggleSrtMenu(h.id)
                                }}
                              >
                                SRT CapCut <span className="tts-menu-arrow">‹</span>
                              </button>
                              {historySrtMenuId === h.id && (
                                <div className="tts-dl-menu tts-dl-submenu" role="menu">
                                  {SRT_STYLE_OPTIONS.map((opt) => (
                                    <button
                                      key={opt.id}
                                      type="button"
                                      role="menuitem"
                                      onClick={() => isDesktopApp
                                        ? onReveal(h.id, 'srt', opt.id)
                                        : onDownload(
                                            historyDownloadUrl(h, 'srt', opt.id),
                                            `${(h.title || h.id).slice(0, 40)}-${opt.id}.srt`,
                                          )}
                                    >
                                      {opt.label}
                                    </button>
                                  ))}
                                </div>
                              )}
                            </div>
                            <button
                              type="button"
                              role="menuitem"
                              onClick={() => isDesktopApp
                                ? onReveal(h.id, 'zip')
                                : onDownload(
                                    historyDownloadUrl(h, 'zip'),
                                    `${(h.title || h.id).slice(0, 40)}.zip`,
                                  )}
                            >
                              ZIP (Audio + SRT)
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                    <button type="button" title="Xóa" onClick={() => onDelete(h)}>
                      <IconTrash size={12} />
                    </button>
                  </div>
                </td>
              </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {historyCapped.length > 0 && (
        <div className="tts-pager">
          <span className="tts-pager-info">
            {offset + 1}–{Math.min(offset + HISTORY_PAGE_SIZE, historyCapped.length)}
            {' / '}
            {historyCapped.length}
            {historyCapped.length >= HISTORY_MAX ? ` (tối đa ${HISTORY_MAX})` : ''}
          </span>
          <div className="tts-pager-btns">
            <button
              type="button"
              className="tts-btn tts-btn-ghost"
              disabled={pageSafe <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              Trước
            </button>
            <span className="tts-pager-page">
              Trang {pageSafe}/{totalPages}
            </span>
            <button
              type="button"
              className="tts-btn tts-btn-ghost"
              disabled={pageSafe >= totalPages}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            >
              Sau
            </button>
          </div>
        </div>
      )}
    </>
  )
}
