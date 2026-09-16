import { useCallback, useEffect, useState } from 'react'
import { api } from '@/features/project/project.api'
import type { RenderedVideo } from '@/features/project/project.types'
import { BackTitle } from '@/shared/components/BackTitle'
import { IconDownload, IconFilm, IconRefresh } from '@/shared/components/Icons'
import { MediaPreviewModal } from '@/shared/components/MediaPreviewModal'
import { localize, useLocale } from '@/app/i18n'
import { toast } from 'sonner'
import './RendersPage.css'

const PAGE_SIZE_OPTIONS = [12, 24, 48] as const
type PageSize = typeof PAGE_SIZE_OPTIONS[number]
const DEFAULT_PAGE_SIZE: PageSize = PAGE_SIZE_OPTIONS[0]

function durationLabel(seconds: number) {
  const total = Math.max(0, Math.round(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
    : `${minutes}:${String(secs).padStart(2, '0')}`
}

function sizeLabel(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / 1024 / 1024).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`
}

function renderName(item: RenderedVideo) {
  return item.name?.trim() || `Render ${item.projectId}`
}

export default function RendersPage({ onBack, onEdit }: { onBack: () => void; onEdit: (projectId: string) => Promise<void> }) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [items, setItems] = useState<RenderedVideo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [canReveal, setCanReveal] = useState(false)
  const [viewing, setViewing] = useState<RenderedVideo | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<PageSize>(DEFAULT_PAGE_SIZE)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [nameDraft, setNameDraft] = useState('')
  const [openingId, setOpeningId] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<'all' | 'video' | 'image' | 'audio' | 'srt'>('all')
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  useEffect(() => { setPage(1); setSelectedIds(new Set()) }, [activeTab])

  const filteredItems = items.filter(item => activeTab === 'all' || item.type === activeTab || (!item.type && activeTab === 'video'))
  const totalPages = Math.max(1, Math.ceil(filteredItems.length / pageSize))
  const safePage = Math.min(page, totalPages)
  const pageItems = filteredItems.slice((safePage - 1) * pageSize, safePage * pageSize)

  const currentViewingIndex = viewing ? filteredItems.findIndex((i) => i.renderId === viewing.renderId) : -1
  const moveViewing = useCallback((delta: number) => {
    if (filteredItems.length <= 1 || currentViewingIndex < 0) return
    const nextIdx = (currentViewingIndex + delta + filteredItems.length) % filteredItems.length
    setViewing(filteredItems[nextIdx])
  }, [filteredItems, currentViewingIndex])

  function getThumbIcon(type?: string) {
    if (type === 'image') return <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
    if (type === 'audio') return <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
    if (type === 'srt') return <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
    return <IconFilm size={32} />
  }

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await api.renders()
      setItems(result.items)
      setCanReveal(result.canReveal)
      setPage(1)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Không tải được danh sách render')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (!viewing) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setViewing(null)
      if (event.key === 'ArrowLeft') moveViewing(-1)
      if (event.key === 'ArrowRight') moveViewing(1)
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [viewing, moveViewing])

  async function reveal(renderId: string) {
    try {
      await api.revealRender(renderId)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Không mở được thư mục')
    }
  }

  async function saveName(item: RenderedVideo) {
    const name = nameDraft.trim()
    if (!name || name === renderName(item)) {
      setEditingId(null)
      return
    }
    try {
      const saved = await api.renameRender(item.renderId, name)
      setItems((current) => current.map((row) => row.renderId === item.renderId ? { ...row, name: saved.name } : row))
      setEditingId(null)
      toast.success(t('Đã đổi tên video.', 'Video renamed.'))
    } catch (err) {
      const msg = err instanceof Error ? err.message : t('Không đổi được tên render', 'Could not rename render')
      setError(msg)
      toast.error(msg)
    }
  }

  async function deleteRender(item: RenderedVideo) {
    if (!window.confirm(t(`Xóa video “${renderName(item)}”? Thao tác này không thể hoàn tác.`, `Delete video “${renderName(item)}”? This action cannot be undone.`))) return
    try {
      await api.deleteRender(item.renderId)
      setItems((current) => current.filter((row) => row.renderId !== item.renderId))
      if (viewing?.renderId === item.renderId) setViewing(null)
      toast.success(t('Đã xóa video.', 'Video deleted.'))
    } catch (err) {
      const msg = err instanceof Error ? err.message : t('Không xóa được video render', 'Could not delete render')
      setError(msg)
      toast.error(msg)
    }
  }

  async function deleteSelected() {
    if (selectedIds.size === 0) return
    if (!window.confirm(t(`Xóa ${selectedIds.size} mục đã chọn? Thao tác này không thể hoàn tác.`, `Delete ${selectedIds.size} selected items? This action cannot be undone.`))) return
    setLoading(true)
    try {
      await api.deleteRendersBatch(Array.from(selectedIds))
      toast.success(t('Đã xóa các mục được chọn.', 'Deleted selected items.'))
      setSelectedIds(new Set())
      await load()
    } catch (err) {
      const msg = err instanceof Error ? err.message : t('Không thể xóa các mục đã chọn', 'Could not delete selected items')
      setError(msg)
      toast.error(msg)
    } finally {
      setLoading(false)
    }
  }

  async function deleteAll() {
    const count = filteredItems.length
    if (count === 0) return
    const msg = activeTab === 'all'
      ? t(`Xóa toàn bộ ${count} file đã render? Thao tác này không thể hoàn tác.`, `Delete all ${count} rendered media? This action cannot be undone.`)
      : t(`Xóa toàn bộ ${count} file trong mục “${activeTab}”? Thao tác này không thể hoàn tác.`, `Delete all ${count} items in “${activeTab}”? This action cannot be undone.`)
    if (!window.confirm(msg)) return
    setLoading(true)
    try {
      const idsToDelete = filteredItems.map(item => item.renderId)
      await api.deleteRendersBatch(idsToDelete, false, activeTab)
      toast.success(t('Đã xóa toàn bộ media.', 'Deleted all media.'))
      setSelectedIds(new Set())
      await load()
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : t('Không thể xóa toàn bộ media', 'Could not delete all media')
      setError(errMsg)
      toast.error(errMsg)
    } finally {
      setLoading(false)
    }
  }

  async function editRender(item: RenderedVideo) {
    setOpeningId(item.renderId)
    setError('')
    try {
      await onEdit(item.projectId)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Không mở lại được project trong editor')
      setOpeningId(null)
    }
  }

  return (
    <main className="renders-page">
      <header className="renders-head">
        <div>
          <BackTitle onBack={onBack}>{t('List render', 'Render list')}</BackTitle>
          <p>{filteredItems.length
            ? t(`${filteredItems.length} media · mới nhất trước`, `${filteredItems.length} media · newest first`)
            : t('Tất cả media xuất từ Clone, Review và công cụ sẽ xuất hiện tại đây.', 'All media exported from Clone, Review, and tools will appear here.')}</p>
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          {selectedIds.size > 0 && (
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center', background: 'var(--border, #e2e8f0)', padding: '6px 12px', borderRadius: '8px' }}>
              <span style={{ fontSize: '0.8rem', fontWeight: 650, color: 'var(--ink, #172033)' }}>{t(`Đã chọn ${selectedIds.size}`, `Selected ${selectedIds.size}`)}</span>
              <button type="button" onClick={() => setSelectedIds(new Set())} style={{ background: 'none', border: 'none', color: 'var(--muted-foreground, #64748b)', cursor: 'pointer', fontSize: '0.8rem', fontWeight: 600 }}>{t('Bỏ chọn', 'Deselect')}</button>
              <button type="button" onClick={() => setSelectedIds(new Set(filteredItems.map(i => i.renderId)))} style={{ background: 'none', border: 'none', color: 'var(--primary, #2684d9)', cursor: 'pointer', fontSize: '0.8rem', fontWeight: 600 }}>{t('Chọn tất cả', 'Select all')}</button>
              <button type="button" onClick={() => void deleteSelected()} style={{ background: '#ef4444', border: 'none', color: '#fff', cursor: 'pointer', fontSize: '0.8rem', padding: '4px 10px', borderRadius: '6px', fontWeight: 650, marginLeft: '4px' }}>{t('Xóa đã chọn', 'Delete selected')}</button>
            </div>
          )}
          <div className="drawing-preview-tabs" style={{ margin: 0 }}>
            <button type="button" className={activeTab === 'all' ? 'is-active' : ''} onClick={() => setActiveTab('all')}>{t('Tất cả', 'All')}</button>
            <button type="button" className={activeTab === 'video' ? 'is-active' : ''} onClick={() => setActiveTab('video')}>{t('Video', 'Video')}</button>
            <button type="button" className={activeTab === 'image' ? 'is-active' : ''} onClick={() => setActiveTab('image')}>{t('Ảnh', 'Image')}</button>
            <button type="button" className={activeTab === 'audio' ? 'is-active' : ''} onClick={() => setActiveTab('audio')}>{t('Âm thanh', 'Audio')}</button>
            <button type="button" className={activeTab === 'srt' ? 'is-active' : ''} onClick={() => setActiveTab('srt')}>{t('Phụ đề', 'Subtitles')}</button>
          </div>
          {filteredItems.length > 0 && selectedIds.size === 0 && (
            <button
              type="button"
              className="renders-delete-all"
              onClick={() => void deleteAll()}
              disabled={loading}
              title={t('Xóa toàn bộ media trong danh sách này', 'Delete all media in this list')}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
              {t('Xóa toàn bộ', 'Delete all')}
            </button>
          )}
          <button type="button" className="renders-refresh" onClick={() => void load()} disabled={loading}>
            <IconRefresh size={16} /> {t('Làm mới', 'Refresh')}
          </button>
        </div>
      </header>

      {error && <div className="renders-alert">{error} <button type="button" onClick={() => void load()}>Thử lại</button></div>}
      {loading ? (
        <div className="renders-state">Đang tải danh sách…</div>
      ) : filteredItems.length === 0 ? (
        <div className="renders-state renders-empty"><IconFilm size={36} /><strong>{t('Chưa có media đã render', 'No rendered media yet')}</strong><span>{t('Xuất media từ Clone, Review hoặc công cụ để xem tại đây.', 'Export media from Clone, Review, or a tool to see it here.')}</span></div>
      ) : (
        <section className="renders-grid" aria-label={t('Danh sách media đã render', 'Rendered media list')}>
          {pageItems.map((item) => (
            <article className={`render-card ${selectedIds.has(item.renderId) ? 'is-selected' : ''}`} key={item.renderId}>
              <input type="checkbox" className="render-select-cb" checked={selectedIds.has(item.renderId)} onChange={(e) => {
                const s = new Set(selectedIds)
                if (e.target.checked) s.add(item.renderId)
                else s.delete(item.renderId)
                setSelectedIds(s)
              }} aria-label="Chọn item" />
              <button type="button" className="render-thumb" onClick={() => setViewing(item)} aria-label={`Xem video ${item.renderId}`}>
                <img src={item.thumbnailUrl} alt="" loading="lazy" onError={(event) => { event.currentTarget.hidden = true }} />
                <span className="render-thumb-fallback">{getThumbIcon(item.type)}</span>
                <span className="render-play">▶</span>
                <time>{durationLabel(item.duration)}</time>
              </button>
              <div className="render-info">
                {editingId === item.renderId ? (
                  <input
                    className="render-name-input"
                    value={nameDraft}
                    maxLength={120}
                    autoFocus
                    aria-label="Tên bản render"
                    onChange={(event) => setNameDraft(event.target.value)}
                    onBlur={() => void saveName(item)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') event.currentTarget.blur()
                      if (event.key === 'Escape') {
                        // Thoát edit trực tiếp: blur() sẽ chạy saveName với
                        // nameDraft cũ trong closure → vẫn đổi tên dù đã huỷ.
                        setNameDraft(renderName(item))
                        setEditingId(null)
                      }
                    }}
                  />
                ) : (
                  <button
                    type="button"
                    className="render-name"
                    title="Bấm để đổi tên"
                    onClick={() => { setEditingId(item.renderId); setNameDraft(renderName(item)) }}
                  >
                    {renderName(item)}
                  </button>
                )}
                <span>{new Intl.DateTimeFormat('vi-VN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(item.createdAt))}</span>
                <span>{item.width}×{item.height} · {sizeLabel(item.sizeBytes)}</span>
                {item.outputFolder && <span className="render-folder" title={item.outputFolder}>{t('Thư mục', 'Folder')}: {item.outputFolder}</span>}
              </div>
              <div className="render-actions">
                <button type="button" onClick={() => setViewing(item)}>Xem</button>
                {canReveal ? (
                  <button type="button" onClick={() => void reveal(item.renderId)}>Mở thư mục</button>
                ) : (
                  <a href={item.downloadUrl} download={`zm-ai-tool-${item.renderId}.mp4`}><IconDownload size={14} /> Tải xuống</a>
                )}
                {item.canEdit !== false && item.projectId && item.projectId !== 'srt' && <button type="button" disabled={openingId === item.renderId} onClick={() => void editRender(item)}>
                  {openingId === item.renderId ? 'Đang mở…' : 'Sửa'}
                </button>}
                <button type="button" className="render-delete" onClick={() => void deleteRender(item)}>Xóa</button>
              </div>
            </article>
          ))}
        </section>
      )}

      {!loading && filteredItems.length > 0 && (
        <nav className="renders-pagination" aria-label={t('Phân trang media đã render', 'Rendered media pagination')}>
          <label className="renders-page-size">
            <span>{t('Hiển thị', 'Show')}</span>
            <select
              value={pageSize}
              aria-label={t('Số mục mỗi trang', 'Items per page')}
              onChange={(event) => {
                setPageSize(Number(event.target.value) as PageSize)
                setPage(1)
              }}
            >
              {PAGE_SIZE_OPTIONS.map((size) => <option key={size} value={size}>{size}</option>)}
            </select>
            <span>{t('mục/trang', 'per page')}</span>
          </label>
          {totalPages > 1 && <>
            <button type="button" disabled={safePage === 1} onClick={() => setPage((current) => Math.max(1, current - 1))}>{t('Trước', 'Previous')}</button>
            <span>{t(`Trang ${safePage}/${totalPages}`, `Page ${safePage}/${totalPages}`)}</span>
            <button type="button" disabled={safePage === totalPages} onClick={() => setPage((current) => Math.min(totalPages, current + 1))}>{t('Sau', 'Next')}</button>
          </>}
        </nav>
      )}

      <MediaPreviewModal
        open={Boolean(viewing)}
        onClose={() => setViewing(null)}
        item={
          viewing
            ? {
                id: viewing.renderId,
                title: renderName(viewing),
                subtitle: [
                  new Intl.DateTimeFormat('vi-VN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(viewing.createdAt)),
                  viewing.width && viewing.height ? `${viewing.width}×${viewing.height}` : '',
                  viewing.sizeBytes ? sizeLabel(viewing.sizeBytes) : '',
                  viewing.duration ? durationLabel(viewing.duration) : '',
                ].filter(Boolean).join(' · '),
                src: viewing.videoUrl,
                downloadUrl: viewing.downloadUrl || viewing.videoUrl,
                downloadFilename: viewing.name
                  ? `${viewing.name}.${viewing.type === 'image' ? 'png' : viewing.type === 'audio' ? 'mp3' : viewing.type === 'srt' ? 'srt' : 'mp4'}`
                  : `media-${viewing.renderId}`,
                type: viewing.type || 'video',
              }
            : null
        }
        totalCount={filteredItems.length}
        currentIndex={currentViewingIndex}
        onPrevious={() => moveViewing(-1)}
        onNext={() => moveViewing(1)}
        onReveal={canReveal && viewing ? () => void reveal(viewing.renderId) : undefined}
        onEdit={
          viewing && viewing.canEdit !== false && viewing.projectId && viewing.projectId !== 'srt'
            ? () => {
                const target = viewing
                setViewing(null)
                void editRender(target)
              }
            : undefined
        }
        editLabel={openingId === viewing?.renderId ? t('Đang mở…', 'Opening…') : t('Sửa dự án', 'Edit project')}
        onDelete={
          viewing
            ? async () => {
                const toDelete = viewing
                if (filteredItems.length > 1) {
                  moveViewing(1)
                } else {
                  setViewing(null)
                }
                await deleteRender(toDelete)
              }
            : undefined
        }
      />
    </main>
  )
}
