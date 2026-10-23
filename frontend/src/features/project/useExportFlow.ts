/**
 * Orchestration xuất bản: state kết quả (URL/path/popup), pending refs cho poll,
 * onExport và xử lý «export xong» dùng chung giữa poll + restore.
 */
import { useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import { api } from './project.api'
import type { JobStatus, ProjectSettings, Segment } from './project.types'

export function useExportFlow({
  projectId,
  status,
  setStatus,
  settings,
  segments,
  setSegments,
  busyAt,
  setProgressMinimized,
}: {
  projectId: string | null
  status: JobStatus
  setStatus: Dispatch<SetStateAction<JobStatus>>
  settings: ProjectSettings
  segments: Segment[]
  setSegments: Dispatch<SetStateAction<Segment[]>>
  busyAt: MutableRefObject<number>
  setProgressMinimized: (minimized: boolean) => void
}) {
  const [exportUrl, setExportUrl] = useState<string | null>(null)
  const [exportPath, setExportPath] = useState<string | null>(null)
  const [viewExportSrc, setViewExportSrc] = useState<string | null>(null)
  const [exportSuccessOpen, setExportSuccessOpen] = useState(false)
  const [lastExportedTypes, setLastExportedTypes] = useState({ video: true, audio: false, srt: false, gif: false })
  const pendingExportUrl = useRef<string | null>(null)
  const pendingExportPath = useRef<string | null>(null)

  // Hiện ô Xem/Tải khi đã từng xuất (kể cả vừa dịch lại — bản có thể cũ)
  useEffect(() => {
    if (!projectId || status.running || exportUrl) return
    if (status.outputRel && (status.progress || 0) >= 100) {
      setExportUrl(`/api/projects/${projectId}/output`)
      setExportPath(status.outputRel)
    }
  }, [projectId, status.running, status.step, status.progress, status.outputRel, exportUrl])

  // ESC đóng popup xem export
  useEffect(() => {
    if (!viewExportSrc) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setViewExportSrc(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [viewExportSrc])

  async function onExport(exportSegments?: Segment[], exportEndSec?: number, exportStartSec?: number, renderName = '', settingsOverride?: Partial<ProjectSettings>, coverDataUrl?: string) {
    if (!projectId || status.running) return
    const effectiveSettings = settingsOverride ? { ...settings, ...settingsOverride } : settings
    const segs = Array.isArray(exportSegments) ? exportSegments : segments
    const finalRenderName = renderName.trim() || `Render ${projectId}`
    let res
    try {
      res = await api.export(projectId, effectiveSettings, segs, exportEndSec, exportStartSec, finalRenderName, coverDataUrl)
    } catch (error) {
      // Activation opens its own popup; keep the existing result and timeline intact.
      if (!String(error).includes('ACCESS_REQUIRED')) setStatus(s => ({ ...s, running: false, error: String(error) }))
      return
    }
    setExportUrl(null)
    setExportPath(null)
    setViewExportSrc(null)
    setProgressMinimized(false)
    busyAt.current = Date.now()
    // Tính effectiveSettings trước để message phản ánh đúng loại xuất
    const doVideo = effectiveSettings.exportVideo !== false
    const doAudio = effectiveSettings.exportAudio === true
    const doSrt = effectiveSettings.exportSrt === true
    const doGif = effectiveSettings.exportGif === true
    const audioHint =
      effectiveSettings.processOriginalAudio && effectiveSettings.originalAudioMode === 'no_vocals'
        ? ' · xóa lời'
        : effectiveSettings.processOriginalAudio && effectiveSettings.originalAudioMode === 'vocals'
          ? ' · giữ lời'
          : effectiveSettings.processOriginalAudio && effectiveSettings.originalAudioMode === 'mute'
            ? ' · tắt âm gốc'
            : ''
    // Message rõ ràng theo loại xuất được chọn
    const typeParts: string[] = []
    if (doVideo) typeParts.push('Video')
    if (doAudio) typeParts.push('Audio')
    if (doSrt) typeParts.push('SRT')
    if (doGif) typeParts.push('GIF')
    const typeLabel = typeParts.join(' + ') || 'Video'
    const videoDetail =
      doVideo && effectiveSettings.coverHardsubs && effectiveSettings.burnSubs && effectiveSettings.targetLang !== 'none'
        ? ' (che chữ cũ + chèn bản dịch)'
        : doVideo && effectiveSettings.burnSubs && effectiveSettings.targetLang !== 'none'
          ? effectiveSettings.captionPlacement === 'above'
            ? ' (chèn bản dịch phía trên)'
            : ' (chèn bản dịch phía dưới)'
          : doVideo && effectiveSettings.coverHardsubs
            ? ' (che chữ cũ)'
            : ''
    setStatus({
      step: 'export',
      progress: 0,
      message: `Đang xuất ${typeLabel}${videoDetail}${audioHint}…`,
      running: true,
      error: undefined,
    })
    if (Array.isArray(exportSegments)) {
      setSegments(exportSegments)
    }
    pendingExportUrl.current = res.url
    pendingExportPath.current = res.exports || res.path || null
    setStatus((s) => ({ ...s, running: true }))
  }

  /** Poll báo export xong — chốt URL/path + mở popup thành công. */
  function applyExportDone(s: JobStatus, pid: string) {
    const url = pendingExportUrl.current || `/api/projects/${pid}/output`
    setExportUrl(url)
    setExportPath(
      pendingExportPath.current ||
        s.outputRel ||
        `backend/public/exports/${pid}.mp4`,
    )
    pendingExportUrl.current = null
    pendingExportPath.current = null
    // Hiện popup xuất xong thay vì auto-play video
    const msg = s.message || ''
    setLastExportedTypes({
      video: /video/i.test(msg),
      audio: /audio|âm thanh|mp3|wav/i.test(msg),
      srt: /srt|chú thích/i.test(msg),
      gif: /gif/i.test(msg),
    })
    setViewExportSrc(s.outputRel ? `${url}?t=${Date.now()}` : null)
    setExportSuccessOpen(true)
  }

  async function onRevealOutput() {
    if (!projectId) return
    try {
      const res = await api.revealOutput(projectId)
      setExportPath(res.path)
    } catch (e) {
      setStatus((s) => ({
        ...s,
        message: e instanceof Error ? e.message : 'Không mở được thư mục',
      }))
    }
  }

  function onViewExport() {
    if (!projectId) return
    setViewExportSrc(`/api/projects/${projectId}/output?t=${Date.now()}`)
    setExportSuccessOpen(true)
  }

  return {
    exportUrl,
    setExportUrl,
    exportPath,
    setExportPath,
    viewExportSrc,
    setViewExportSrc,
    exportSuccessOpen,
    setExportSuccessOpen,
    lastExportedTypes,
    pendingExportUrl,
    applyExportDone,
    onExport,
    onRevealOutput,
    onViewExport,
  }
}
