/**
 * Nhóm kéo-thả timeline (clip caption/TTS, media, text, marquee, bbox che chữ)
 * — tách từ LivePreviewEditor. State draft/selection sống ở component; hook chỉ
 * giữ các handler pointer để component gọn lại, hành vi giữ nguyên.
 */
import type { PointerEvent as ReactPointerEvent } from 'react'
import type { ProjectSettings, Segment, TextOverlay } from '@/features/project/project.types'
import { layoutOcrOverlay } from '@/features/editor/ocrOverlayLayout'
import {
  type MediaClip,
  type PixelBox,
  type PropTab,
  type SnapGuides,
  type TrackId,
  MIN_CLIP_SEC,
  autoFontFromBbox,
  captionLaneOf,
  clampCoverBox,
  effectiveOverlayLayout,
  isOcrOverlayLayout,
  reindexSegments,
  resolveOverlayFontPreferred,
  resolvePreviewOverLayout,
  segmentWithLayout,
  snapBoxToCenter,
} from '@/features/editor/lib'

type Box = { x: number; y: number; w: number; h: number }
type DraftRange = { id: string; start: number; end: number }
type GroupDraft = Record<string, { start: number; end: number }>
type MarqueeBox = { x0: number; y0: number; x1: number; y1: number }

type TimelineDragDeps = {
  segments: Segment[]
  overlays: TextOverlay[]
  settings: ProjectSettings
  busy: boolean
  timelineEditLocked: boolean
  trackLocked: Record<TrackId, boolean>
  timelineDuration: number
  pxPerSec: number
  time: number
  tool: 'select' | 'cover' | 'text'
  autoSnapping: boolean
  mediaLinked: boolean
  sourceWidth: number
  sourceHeight: number
  crop: Box
  trackFocus: TrackId
  selected: Segment | undefined
  selectedId: string | null
  selectedIds: string[]
  selectedMediaIds: string[]
  selectedOverlayIds: string[]
  selectedBox: PixelBox | null
  fallbackBox: PixelBox
  bboxDraft: Box | null
  applyCaptionToAll: boolean
  videoClips: MediaClip[]
  bgClips: MediaClip[]
  // Refs (component sở hữu — còn dùng ở effect/JSX khác)
  tracksScrollRef: React.RefObject<HTMLDivElement | null>
  tracksColRef: React.RefObject<HTMLDivElement | null>
  canvasRef: React.RefObject<HTMLDivElement | null>
  draftRef: React.RefObject<DraftRange | null>
  groupDraftRef: React.RefObject<GroupDraft | null>
  marqueeRef: React.RefObject<(MarqueeBox & { additive: boolean; active: boolean }) | null>
  bboxDraftRef: React.RefObject<Box | null>
  // Setters
  setDraft: React.Dispatch<React.SetStateAction<DraftRange | null>>
  setGroupDraft: React.Dispatch<React.SetStateAction<GroupDraft | null>>
  setMarquee: React.Dispatch<React.SetStateAction<MarqueeBox | null>>
  setBboxDraft: React.Dispatch<React.SetStateAction<Box | null>>
  setDraggingBox: React.Dispatch<React.SetStateAction<boolean>>
  setSnapGuides: React.Dispatch<React.SetStateAction<SnapGuides>>
  setSelectedId: React.Dispatch<React.SetStateAction<string | null>>
  setSelectedIds: React.Dispatch<React.SetStateAction<string[]>>
  setSelectedMediaId: React.Dispatch<React.SetStateAction<string | null>>
  setSelectedMediaIds: React.Dispatch<React.SetStateAction<string[]>>
  setSelectedDubIds: React.Dispatch<React.SetStateAction<string[]>>
  setSelectedOverlayId: React.Dispatch<React.SetStateAction<string | null>>
  setSelectedOverlayIds: React.Dispatch<React.SetStateAction<string[]>>
  setTrackFocus: React.Dispatch<React.SetStateAction<TrackId>>
  setPropTab: React.Dispatch<React.SetStateAction<PropTab>>
  setVideoClips: React.Dispatch<React.SetStateAction<MediaClip[]>>
  setBgClips: React.Dispatch<React.SetStateAction<MediaClip[]>>
  // Hàm component
  expandGroupSelection: (ids: string[]) => string[]
  focusCaption: (seg: Segment, opts?: { additive?: boolean; range?: boolean }) => void
  focusDub: (seg: Segment, opts?: { keepMulti?: boolean }) => void
  focusVideo: (clipId?: string) => void
  focusBg: (clipId?: string) => void
  focusText: (overlayId: string) => void
  seekPlayhead: (next: number) => void
  pushHistory: () => void
  pushHistoryOnce: (gate: { current: boolean }) => void
  editSegment: (next: Segment, opts?: { textField?: string; skipHistory?: boolean }) => void | Promise<void>
  editOverlay: (
    overlay: TextOverlay,
    isNew?: boolean,
    opts?: { textField?: boolean; skipHistory?: boolean },
  ) => void | Promise<void>
  getCachedPreviewLayout: (
    s: Segment,
    override?: PixelBox,
  ) => ReturnType<typeof resolvePreviewOverLayout>
  onSegmentsReplace: (segments: Segment[], opts?: { persist?: boolean }) => void | Promise<void>
  onOverlaysReplace: (overlays: TextOverlay[]) => void | Promise<void>
}

export function useTimelineDrag(deps: TimelineDragDeps) {
  const {
    segments,
    overlays,
    settings,
    busy,
    timelineEditLocked,
    trackLocked,
    timelineDuration,
    pxPerSec,
    time,
    tool,
    autoSnapping,
    mediaLinked,
    sourceWidth,
    sourceHeight,
    crop,
    trackFocus,
    selected,
    selectedIds,
    selectedMediaIds,
    selectedOverlayIds,
    selectedBox,
    fallbackBox,
    bboxDraft,
    applyCaptionToAll,
    videoClips,
    bgClips,
    tracksScrollRef,
    tracksColRef,
    canvasRef,
    draftRef,
    groupDraftRef,
    marqueeRef,
    bboxDraftRef,
    setDraft,
    setGroupDraft,
    setMarquee,
    setBboxDraft,
    setDraggingBox,
    setSnapGuides,
    setSelectedId,
    setSelectedIds,
    setSelectedMediaId,
    setSelectedMediaIds,
    setSelectedDubIds,
    setSelectedOverlayId,
    setSelectedOverlayIds,
    setTrackFocus,
    setPropTab,
    setVideoClips,
    setBgClips,
    expandGroupSelection,
    focusCaption,
    focusDub,
    focusVideo,
    focusBg,
    focusText,
    seekPlayhead,
    pushHistory,
    pushHistoryOnce,
    editSegment,
    editOverlay,
    getCachedPreviewLayout,
    onSegmentsReplace,
    onOverlaysReplace,
  } = deps

  function commitCaptionBoxResult(next: Segment, skipHistory: boolean) {
    // Moving text must not create a source mask for captions without OCR.
    const original = segments.find((segment) => segment.id === next.id)
    next = { ...next, bbox: original?.bbox ?? null, coverBox: original?.coverBox ?? null }
    if (!applyCaptionToAll || (!next.captionBox && !next.bbox)) {
      editSegment(next, { skipHistory })
      return
    }
    if (!skipHistory) {
      pushHistory()
    }
    const box = next.captionBox ?? next.bbox!
    const lane = captionLaneOf(next, sourceHeight, sourceWidth)
    const all = segments.map((peer) => {
      if (!(peer.translation || '').trim() || captionLaneOf(peer, sourceHeight, sourceWidth) !== lane) return peer
      const overlayLay = effectiveOverlayLayout(peer, sourceHeight, sourceWidth)
        ?? (isOcrOverlayLayout(peer.layout) ? peer.layout : null)
      if (overlayLay && settings.burnSubs) {
        const laid = layoutOcrOverlay(
          overlayLay,
          box,
          peer.translation,
          resolveOverlayFontPreferred(peer),
          sourceWidth,
          sourceHeight,
        )
        const result = segmentWithLayout({ ...peer, captionBox: box }, {
          cover: box,
          caption: laid.caption,
          lines: laid.lines,
          fontPx: laid.fontPx,
        }, laid.fontPx)
        return { ...result, bbox: peer.bbox ?? null, coverBox: peer.coverBox ?? null }
      }
      if (settings.burnSubs) {
        const laid = getCachedPreviewLayout(peer, box)
        if (laid) {
          const fontPx = laid.fontPx ?? autoFontFromBbox(laid.cover, peer.translation, 0)
          const result = segmentWithLayout({ ...peer, captionBox: box }, laid, fontPx)
          return { ...result, bbox: peer.bbox ?? null, coverBox: peer.coverBox ?? null }
        }
      }
      return { ...peer, captionBox: box, captionLayout: null }
    })
    void onSegmentsReplace(all)
  }

  /**
   * Kéo clip segment (Caption / TTS) — CapCut free:
   * move/start/end trong [0, timeline]; cho chồng/gap; multi-move cả selection.
   */
  function beginDrag(event: ReactPointerEvent, segment: Segment, mode: 'move' | 'start' | 'end') {
    if (timelineEditLocked || trackLocked.caption) return
    event.preventDefault()
    event.stopPropagation()
    let moveIds = selectedIds.includes(segment.id)
      ? expandGroupSelection(selectedIds)
      : expandGroupSelection([segment.id])
    if (selectedIds.includes(segment.id) && moveIds.length > selectedIds.length) {
      setSelectedIds(moveIds)
    }
    const multi =
      mode === 'move'
      && moveIds.length > 1
      && moveIds.includes(segment.id)
    if (!multi) {
      if (!selectedIds.includes(segment.id) || selectedIds.length <= 1) {
        if (trackFocus === 'dub') focusDub(segment)
        else focusCaption(segment)
        moveIds = expandGroupSelection([segment.id])
      } else {
        setSelectedId(segment.id)
      }
    } else {
      setSelectedId(segment.id)
      setSelectedIds(moveIds)
    }
    const original = { start: segment.start, end: segment.end }
    const minDuration = 0.12
    const maxT = Math.max(timelineDuration, segment.end, 1)

    // ── Group move (free — chỉ clamp mép timeline) ──
    if (multi) {
      const group = segments.filter((s) => moveIds.includes(s.id))
      if (group.length >= 2) {
        const origins = Object.fromEntries(
          group.map((s) => [s.id, { start: s.start, end: s.end }]),
        )
        const gStart = Math.min(...group.map((s) => s.start))
        const gEnd = Math.max(...group.map((s) => s.end))
        const span = gEnd - gStart

        const update = (move: PointerEvent) => {
          let delta = (move.clientX - event.clientX) / pxPerSec
          let ns = gStart + delta
          ns = Math.max(0, Math.min(maxT - span, ns))
          delta = ns - gStart
          const next: Record<string, { start: number; end: number }> = {}
          for (const s of group) {
            const o = origins[s.id]
            next[s.id] = {
              start: Math.max(0, o.start + delta),
              end: Math.min(maxT, o.end + delta),
            }
          }
          groupDraftRef.current = next
          setGroupDraft(next)
        }
        const commit = () => {
          window.removeEventListener('pointermove', update)
          window.removeEventListener('pointerup', commit)
          const cur = groupDraftRef.current
          groupDraftRef.current = null
          setGroupDraft(null)
          if (!cur) return
          const changed = Object.keys(cur).some((id) => {
            const o = origins[id]
            return Math.abs(cur[id].start - o.start) > 0.001
          })
          if (!changed) return
          pushHistory()
          const nextSegs = segments.map((s) => {
            const d = cur[s.id]
            return d ? { ...s, start: d.start, end: d.end } : s
          })
          void onSegmentsReplace(reindexSegments(nextSegs))
        }
        window.addEventListener('pointermove', update)
        window.addEventListener('pointerup', commit, { once: true })
        return
      }
    }

    // ── Single — free move / trim ──
    const update = (move: PointerEvent) => {
      const delta = (move.clientX - event.clientX) / pxPerSec
      let start = original.start
      let end = original.end
      const dur = original.end - original.start
      if (mode === 'move') {
        start = Math.max(0, Math.min(maxT - dur, original.start + delta))
        end = start + dur
      } else if (mode === 'start') {
        start = Math.max(0, Math.min(original.end - minDuration, original.start + delta))
      } else {
        end = Math.min(maxT, Math.max(original.start + minDuration, original.end + delta))
      }
      const next = { id: segment.id, start, end }
      draftRef.current = next
      setDraft(next)
    }

    const commit = () => {
      window.removeEventListener('pointermove', update)
      window.removeEventListener('pointerup', commit)
      const current = draftRef.current
      draftRef.current = null
      setDraft(null)
      if (
        current?.id === segment.id &&
        (Math.abs(current.start - original.start) > 0.001 || Math.abs(current.end - original.end) > 0.001)
      ) {
        editSegment({ ...segment, start: current.start, end: current.end })
      }
    }
    window.addEventListener('pointermove', update)
    window.addEventListener('pointerup', commit, { once: true })
  }

  function snapMediaRange(
    track: 'video' | 'bg',
    clipId: string,
    mode: 'move' | 'start' | 'end',
    start: number,
    end: number,
  ) {
    if (!autoSnapping || pxPerSec <= 0) return { start, end }
    const list = track === 'video' ? videoClips : bgClips
    const points = [0, time, timelineDuration]
    for (const clip of list) {
      if (clip.id !== clipId) points.push(clip.start, clip.end)
    }
    const threshold = 8 / pxPerSec
    const nearest = (value: number) => points.reduce(
      (best, point) => Math.abs(point - value) < Math.abs(best - value) ? point : best,
      value,
    )
    if (mode === 'start') {
      const point = nearest(start)
      return { start: Math.abs(point - start) <= threshold ? point : start, end }
    }
    if (mode === 'end') {
      const point = nearest(end)
      return { start, end: Math.abs(point - end) <= threshold ? point : end }
    }
    const startPoint = nearest(start)
    const endPoint = nearest(end)
    const startDelta = startPoint - start
    const endDelta = endPoint - end
    const delta = Math.abs(startDelta) <= Math.abs(endDelta) ? startDelta : endDelta
    return Math.abs(delta) <= threshold ? { start: start + delta, end: end + delta } : { start, end }
  }

  /** Kéo clip Video / Âm gốc (media) — move + trim + auto snap. */
  function beginMediaDrag(
    event: ReactPointerEvent,
    track: 'video' | 'bg',
    clip: MediaClip,
    mode: 'move' | 'start' | 'end',
  ) {
    if (timelineEditLocked || trackLocked[track]) return
    event.preventDefault()
    event.stopPropagation()
    if (track === 'video') focusVideo(clip.id)
    else focusBg(clip.id)
    const original = { start: clip.start, end: clip.end }
    const minDuration = MIN_CLIP_SEC
    const maxT = Math.max(timelineDuration, clip.end, 1)
    const list = track === 'video' ? videoClips : bgClips
    const setList = track === 'video' ? setVideoClips : setBgClips

    // Multi media move
    const multiIds =
      mode === 'move' && selectedMediaIds.includes(clip.id) && selectedMediaIds.length > 1
        ? selectedMediaIds
        : [clip.id]
    if (multiIds.length > 1) {
      const group = list.filter((c) => multiIds.includes(c.id))
      const origins = Object.fromEntries(group.map((c) => [c.id, { start: c.start, end: c.end }]))
      const gStart = Math.min(...group.map((c) => c.start))
      const gEnd = Math.max(...group.map((c) => c.end))
      const span = gEnd - gStart
      const update = (move: PointerEvent) => {
        let delta = (move.clientX - event.clientX) / pxPerSec
        let ns = Math.max(0, Math.min(maxT - span, gStart + delta))
        delta = ns - gStart
        const next: Record<string, { start: number; end: number }> = {}
        for (const c of group) {
          const o = origins[c.id]
          next[c.id] = {
            start: Math.max(0, o.start + delta),
            end: Math.min(maxT, o.end + delta),
          }
        }
        groupDraftRef.current = next
        setGroupDraft(next)
      }
      const commit = () => {
        window.removeEventListener('pointermove', update)
        window.removeEventListener('pointerup', commit)
        const cur = groupDraftRef.current
        groupDraftRef.current = null
        setGroupDraft(null)
        if (!cur) return
        const changed = Object.keys(cur).some((id) => {
          const o = origins[id]
          return Math.abs(cur[id].start - o.start) > 0.001 || Math.abs(cur[id].end - o.end) > 0.001
        })
        if (!changed) return
        pushHistory()
        setList((prev) =>
          prev
            .map((c) => (cur[c.id] ? { ...c, start: cur[c.id].start, end: cur[c.id].end } : c))
            .sort((a, b) => a.start - b.start),
        )
      }
      window.addEventListener('pointermove', update)
      window.addEventListener('pointerup', commit, { once: true })
      return
    }

    const update = (move: PointerEvent) => {
      const delta = (move.clientX - event.clientX) / pxPerSec
      let start = original.start
      let end = original.end
      const dur = original.end - original.start
      if (mode === 'move') {
        start = Math.max(0, Math.min(maxT - dur, original.start + delta))
        end = start + dur
      } else if (mode === 'start') {
        start = Math.max(0, Math.min(original.end - minDuration, original.start + delta))
      } else {
        end = Math.min(maxT, Math.max(original.start + minDuration, original.end + delta))
      }
      const snapped = snapMediaRange(track, clip.id, mode, start, end)
      start = snapped.start
      end = snapped.end
      const next = { id: clip.id, start, end }
      draftRef.current = next
      setDraft(next)
    }
    const commit = () => {
      window.removeEventListener('pointermove', update)
      window.removeEventListener('pointerup', commit)
      const current = draftRef.current
      draftRef.current = null
      setDraft(null)
      if (
        current?.id === clip.id
        && (Math.abs(current.start - original.start) > 0.001
          || Math.abs(current.end - original.end) > 0.001)
      ) {
        pushHistory()
        setList((prev) =>
          prev
            .map((c) => (c.id === clip.id ? { ...c, start: current.start, end: current.end } : c))
            .sort((a, b) => a.start - b.start),
        )
        if (track === 'video' && mediaLinked) {
          setBgClips((prev) => prev.map((c) => {
            if (Math.abs(c.start - original.start) > 0.02 || Math.abs(c.end - original.end) > 0.02) return c
            return { ...c, start: current.start, end: current.end }
          }))
        }
      }
    }
    window.addEventListener('pointermove', update)
    window.addEventListener('pointerup', commit, { once: true })
  }

  /** Kéo clip Text trên timeline track. */
  function beginTimelineTextDrag(
    event: ReactPointerEvent,
    overlay: TextOverlay,
    mode: 'move' | 'start' | 'end',
  ) {
    const isWatermark = Boolean(overlay.watermarkSource)
      || overlay.id === 'auto-watermark-ai-generated'
      || overlay.id === 'auto-watermark-static-logo'
    const overlayTrack = isWatermark ? 'watermark' : overlay.track === 'ocr' ? 'ocr' : 'text'
    if (timelineEditLocked || trackLocked[overlayTrack]) return
    event.preventDefault()
    event.stopPropagation()
    focusText(overlay.id)
    const moveIds = selectedOverlayIds.includes(overlay.id)
      ? selectedOverlayIds.filter((id) => overlays.some((item) => item.id === id && item.track === overlay.track))
      : [overlay.id]
    const original = { start: overlay.start, end: overlay.end }
    const minDuration = 0.12
    const maxT = Math.max(timelineDuration, overlay.end, 1)
    if (mode === 'move' && moveIds.length > 1) {
      const group = overlays.filter((item) => moveIds.includes(item.id))
      const origins = Object.fromEntries(group.map((item) => [item.id, { start: item.start, end: item.end }]))
      const groupStart = Math.min(...group.map((item) => item.start))
      const groupEnd = Math.max(...group.map((item) => item.end))
      const span = groupEnd - groupStart
      const updateGroup = (move: PointerEvent) => {
        let delta = (move.clientX - event.clientX) / pxPerSec
        const nextStart = Math.max(0, Math.min(Math.max(0, timelineDuration - span), groupStart + delta))
        delta = nextStart - groupStart
        const next = Object.fromEntries(group.map((item) => [item.id, {
          start: origins[item.id].start + delta,
          end: origins[item.id].end + delta,
        }]))
        groupDraftRef.current = next
        setGroupDraft(next)
      }
      const commitGroup = () => {
        window.removeEventListener('pointermove', updateGroup)
        window.removeEventListener('pointerup', commitGroup)
        const current = groupDraftRef.current
        groupDraftRef.current = null
        setGroupDraft(null)
        if (!current) return
        const changed = Object.keys(current).some((id) => {
          const o = origins[id]
          return Math.abs(current[id].start - o.start) > 0.001 || Math.abs(current[id].end - o.end) > 0.001
        })
        if (!changed) return
        pushHistory()
        void onOverlaysReplace(overlays.map((item) => current[item.id] ? { ...item, ...current[item.id] } : item))
      }
      window.addEventListener('pointermove', updateGroup)
      window.addEventListener('pointerup', commitGroup, { once: true })
      return
    }
    const update = (move: PointerEvent) => {
      const delta = (move.clientX - event.clientX) / pxPerSec
      let start = original.start
      let end = original.end
      const dur = original.end - original.start
      if (mode === 'move') {
        start = Math.max(0, Math.min(maxT - dur, original.start + delta))
        end = start + dur
      } else if (mode === 'start') {
        start = Math.max(0, Math.min(original.end - minDuration, original.start + delta))
      } else {
        end = Math.min(maxT, Math.max(original.start + minDuration, original.end + delta))
      }
      const next = { id: overlay.id, start, end }
      draftRef.current = next
      setDraft(next)
    }
    const commit = () => {
      window.removeEventListener('pointermove', update)
      window.removeEventListener('pointerup', commit)
      const current = draftRef.current
      draftRef.current = null
      setDraft(null)
      if (
        current?.id === overlay.id
        && (Math.abs(current.start - original.start) > 0.001
          || Math.abs(current.end - original.end) > 0.001)
      ) {
        editOverlay({ ...overlay, start: current.start, end: current.end })
      }
    }
    window.addEventListener('pointermove', update)
    window.addEventListener('pointerup', commit, { once: true })
  }

  /** Kéo khung chọn — hit Video + Caption + TTS + Âm gốc + Text (CapCut-style). */
  function beginMarqueeSelect(event: ReactPointerEvent<HTMLElement>) {
    if (timelineEditLocked || event.button !== 0) return
    if ((event.target as HTMLElement).closest(
      '[data-caption-clip],[data-media-clip],[data-dub-clip],[data-text-clip]',
    )) return
    const scroller = tracksScrollRef.current
    if (!scroller) return
    const content = scroller.firstElementChild as HTMLElement | null
    if (!content) return
    event.preventDefault()
    event.stopPropagation()
    const crect = content.getBoundingClientRect()
    const x0 = event.clientX - crect.left + scroller.scrollLeft
    const y0 = event.clientY - crect.top + scroller.scrollTop
    const additive = event.ctrlKey || event.metaKey || event.shiftKey
    marqueeRef.current = { x0, y0, x1: x0, y1: y0, additive, active: false }
    setMarquee({ x0, y0, x1: x0, y1: y0 })

    const hitBox = (el: HTMLElement, box: { left: number; top: number; right: number; bottom: number }) => {
      const r = el.getBoundingClientRect()
      const left = r.left - crect.left + scroller.scrollLeft
      const top = r.top - crect.top + scroller.scrollTop
      const right = left + r.width
      const bottom = top + r.height
      return left < box.right && right > box.left && top < box.bottom && bottom > box.top
    }

    const collect = (box: { left: number; top: number; right: number; bottom: number }) => {
      const caps: string[] = []
      const media: string[] = []
      const dubs: string[] = []
      const texts: string[] = []
      content.querySelectorAll<HTMLElement>('[data-caption-clip]').forEach((el) => {
        if (!hitBox(el, box)) return
        const sid = el.getAttribute('data-seg-id')
        if (sid) caps.push(sid)
      })
      content.querySelectorAll<HTMLElement>('[data-media-clip]').forEach((el) => {
        if (!hitBox(el, box)) return
        const mid = el.getAttribute('data-clip-id') || el.getAttribute('data-media-id')
        if (mid) media.push(mid)
      })
      content.querySelectorAll<HTMLElement>('[data-dub-clip]').forEach((el) => {
        if (!hitBox(el, box)) return
        const did = el.getAttribute('data-seg-id')
        if (did) dubs.push(did)
      })
      content.querySelectorAll<HTMLElement>('[data-text-clip]').forEach((el) => {
        if (!hitBox(el, box)) return
        const tid = el.getAttribute('data-overlay-id')
        if (tid) texts.push(tid)
      })
      return {
        caps: expandGroupSelection(caps),
        media: [...new Set(media)],
        dubs: [...new Set(dubs)],
        texts: [...new Set(texts)],
      }
    }

    const applyHits = (
      hits: { caps: string[]; media: string[]; dubs: string[]; texts: string[] },
      additiveHit: boolean,
    ) => {
      if (additiveHit) {
        setSelectedIds((prev) => [...new Set([...prev, ...hits.caps])])
        setSelectedMediaIds((prev) => [...new Set([...prev, ...hits.media])])
        setSelectedDubIds((prev) => [...new Set([...prev, ...hits.dubs])])
        setSelectedOverlayIds((prev) => [...new Set([...prev, ...hits.texts])])
      } else {
        setSelectedIds(hits.caps)
        setSelectedMediaIds(hits.media)
        setSelectedDubIds(hits.dubs)
        setSelectedOverlayIds(hits.texts)
      }
      if (hits.caps.length) {
        setSelectedId(hits.caps[hits.caps.length - 1])
        setTrackFocus('caption')
        setSelectedOverlayId(null)
      } else if (hits.dubs.length) {
        setSelectedId(hits.dubs[hits.dubs.length - 1])
        setTrackFocus('dub')
        setSelectedOverlayId(null)
      } else if (hits.media.length) {
        const mid = hits.media[hits.media.length - 1]
        setSelectedMediaId(mid)
        // video vs bg theo clip list
        const isBg = bgClips.some((c) => c.id === mid)
        setTrackFocus(isBg ? 'bg' : 'video')
        if (!additiveHit) {
          setSelectedId(null)
          setSelectedOverlayId(null)
        }
      } else if (hits.texts.length) {
        const lastId = hits.texts[hits.texts.length - 1]
        setSelectedOverlayId(lastId)
        setTrackFocus(overlays.find((item) => item.id === lastId)?.track === 'ocr' ? 'ocr' : 'text')
        if (!additiveHit) {
          setSelectedId(null)
          setSelectedMediaId(null)
        }
      } else if (!additiveHit) {
        setSelectedId(null)
        setSelectedMediaId(null)
        setSelectedOverlayId(null)
        setSelectedOverlayIds([])
        setSelectedMediaIds([])
        setSelectedDubIds([])
      }
    }

    const update = (move: PointerEvent) => {
      const st = marqueeRef.current
      if (!st) return
      const crect2 = content.getBoundingClientRect()
      const x1 = move.clientX - crect2.left + scroller.scrollLeft
      const y1 = move.clientY - crect2.top + scroller.scrollTop
      if (!st.active && (Math.abs(x1 - st.x0) > 4 || Math.abs(y1 - st.y0) > 4)) {
        st.active = true
      }
      st.x1 = x1
      st.y1 = y1
      marqueeRef.current = st
      setMarquee({ x0: st.x0, y0: st.y0, x1, y1 })
      if (!st.active) return
      const left = Math.min(st.x0, x1)
      const right = Math.max(st.x0, x1)
      const top = Math.min(st.y0, y1)
      const bottom = Math.max(st.y0, y1)
      applyHits(collect({ left, top, right, bottom }), st.additive)
    }

    const commit = (up: PointerEvent) => {
      window.removeEventListener('pointermove', update)
      window.removeEventListener('pointerup', commit)
      const st = marqueeRef.current
      marqueeRef.current = null
      setMarquee(null)
      if (st && !st.active) {
        const sc = tracksScrollRef.current
        const col = tracksColRef.current
        if (sc && col && pxPerSec > 0) {
          const rect = col.getBoundingClientRect()
          const x = up.clientX - rect.left + sc.scrollLeft
          const tt = Math.max(0, Math.min(timelineDuration, x / pxPerSec))
          seekPlayhead(tt)
        }
        if (!st.additive) {
          setSelectedIds([])
          setSelectedMediaIds([])
          setSelectedDubIds([])
        }
      }
    }
    window.addEventListener('pointermove', update)
    window.addEventListener('pointerup', commit, { once: true })
  }

  function beginBboxDrag(
    event: ReactPointerEvent,
    mode: 'move' | 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w',
    targetSeg?: Segment | null,
  ) {
    const seg = targetSeg ?? selected
    event.stopPropagation()
    if (event.button !== 0 || !seg || busy || timelineEditLocked || tool === 'text' || trackLocked.caption) return
    // One visual editor target at a time: selecting a caption bbox must not
    // leave a manual blur/effect looking active underneath it.
    setSelectedOverlayId(null)
    setSelectedOverlayIds([])
    setSelectedId(seg.id)
    setSelectedIds([seg.id])
    setTrackFocus('caption')
    const canvas = canvasRef.current
    if (!canvas) return
    event.preventDefault()
    const target = event.currentTarget
    target.setPointerCapture?.(event.pointerId)
    setPropTab('caption')
    const rect = canvas.getBoundingClientRect()
    // Bắt đầu từ khung đang hiện (selectedBox), không nhảy về OCR raw / fitHardsub
    const original = clampCoverBox(
      bboxDraft ?? selectedBox ?? seg.captionBox ?? seg.bbox ?? fallbackBox,
      sourceWidth,
      sourceHeight,
    )
    const minSize = 12
    const histGate = { current: false }
    bboxDraftRef.current = null
    setDraggingBox(true)
    setSnapGuides({ h: false, v: false })

    const clipBox = (left: number, top: number, right: number, bottom: number): PixelBox => {
      const x = Math.max(0, Math.min(sourceWidth - minSize, left))
      const y = Math.max(0, Math.min(sourceHeight - minSize, top))
      const w = Math.max(minSize, Math.min(sourceWidth - x, right - left))
      const h = Math.max(minSize, Math.min(sourceHeight - y, bottom - top))
      return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) }
    }

    const update = (move: PointerEvent) => {
      if (move.pointerId !== event.pointerId) return
      const dx = ((move.clientX - event.clientX) / rect.width) * crop.w
      const dy = ((move.clientY - event.clientY) / rect.height) * crop.h
      let left = original.x, top = original.y
      let right = original.x + original.w, bottom = original.y + original.h
      if (mode === 'move') {
        left = Math.max(0, Math.min(sourceWidth - original.w, original.x + dx))
        top = Math.max(0, Math.min(sourceHeight - original.h, original.y + dy))
        right = left + original.w; bottom = top + original.h
      } else {
        if (mode.includes('w')) left = Math.max(0, Math.min(right - minSize, original.x + dx))
        if (mode.includes('e')) right = Math.min(sourceWidth, Math.max(left + minSize, right + dx))
        if (mode.includes('n')) top = Math.max(0, Math.min(bottom - minSize, original.y + dy))
        if (mode.includes('s')) bottom = Math.min(sourceHeight, Math.max(top + minSize, bottom + dy))
      }
      let next = clipBox(left, top, right, bottom)
      // Snap tâm chỉ khi gần giữa — Alt giữ = tắt snap (kéo thật sự tự do)
      if (mode === 'move' && !move.altKey) {
        const snapped = snapBoxToCenter(next, sourceWidth, sourceHeight)
        next = snapped.box
        setSnapGuides(snapped.guides)
      } else {
        setSnapGuides({ h: false, v: false })
      }
      if (
        Math.abs(next.x - original.x) > 1
        || Math.abs(next.y - original.y) > 1
        || Math.abs(next.w - original.w) > 1
        || Math.abs(next.h - original.h) > 1
      ) {
        pushHistoryOnce(histGate)
      }
      bboxDraftRef.current = next; setBboxDraft(next)
    }

    const commit = (up: PointerEvent) => {
      if (up.pointerId !== event.pointerId) return
      window.removeEventListener('pointermove', update)
      window.removeEventListener('pointerup', commit)
      window.removeEventListener('pointercancel', commit)
      if (target.hasPointerCapture?.(event.pointerId)) target.releasePointerCapture(event.pointerId)
      setDraggingBox(false)
      setSnapGuides({ h: false, v: false })
      const next = bboxDraftRef.current
      bboxDraftRef.current = null; setBboxDraft(null)
      if (next && histGate.current) {
        const norm = clampCoverBox(next, sourceWidth, sourceHeight)
        const sizeChanged =
          Math.abs(norm.w - original.w) > 2 || Math.abs(norm.h - original.h) > 2
        // mid/dọc/nhãn (+ horizontal giữa khung): cover cố định = khung kéo, fit chữ trong box
        const overlayLay =
          effectiveOverlayLayout(seg, sourceHeight, sourceWidth)
          ?? (isOcrOverlayLayout(seg.layout) ? seg.layout : null)
        if (overlayLay && seg.translation.trim() && settings.burnSubs) {
          const lockFs = resolveOverlayFontPreferred(seg)
          const preferred = sizeChanged
            ? lockFs
            : (lockFs || Number(seg.captionLayout?.fontSize) || 0)
          const laid = layoutOcrOverlay(
            overlayLay,
            norm,
            seg.translation,
            preferred,
            sourceWidth,
            sourceHeight,
          )
          commitCaptionBoxResult(segmentWithLayout({ ...seg, captionBox: norm }, {
            cover: norm,
            caption: laid.caption,
            lines: laid.lines,
            fontPx: laid.fontPx,
          }, laid.fontPx), histGate.current)
          return
        }
        // Caption ngang: commit the exact live-drag layout; do not fit again.
        if (seg.translation.trim() && settings.burnSubs) {
          const live = getCachedPreviewLayout(seg, norm)
          if (live) {
            const fitFs = live.fontPx ?? autoFontFromBbox(live.cover, seg.translation, 0)
            commitCaptionBoxResult(segmentWithLayout({ ...seg, captionBox: norm }, live, fitFs), histGate.current)
          } else {
            commitCaptionBoxResult({ ...seg, captionBox: norm, captionLayout: seg.captionLayout ?? null }, histGate.current)
          }
          return
        }
        commitCaptionBoxResult({ ...seg, captionBox: norm, captionLayout: seg.captionLayout ?? null }, histGate.current)
      }
    }
    window.addEventListener('pointermove', update)
    window.addEventListener('pointerup', commit)
    window.addEventListener('pointercancel', commit)
  }

  return {
    beginDrag,
    beginMediaDrag,
    beginTimelineTextDrag,
    beginMarqueeSelect,
    beginBboxDrag,
  }
}
