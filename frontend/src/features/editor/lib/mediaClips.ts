import type { Segment, TextOverlay } from '@/features/project/project.types'
import { MIN_CLIP_SEC, SPLIT_EDGE } from './timelineBasics'

/** Clip Video / Âm gốc trên timeline (tách khỏi Caption·TTS) */
export type MediaClip = { id: string; start: number; end: number; sourceStart?: number }

export function fullMediaClip(end: number): MediaClip {
  return { id: crypto.randomUUID(), start: 0, end: Math.max(end, MIN_CLIP_SEC), sourceStart: 0 }
}

/**
 * Cho timeline theo file hiện tại, trừ khi người dùng đã thật sự trim Video.
 * Khi đổi Preview 20s → full, clip Video mặc định từng chạm mép 20s phải nở
 * theo file full; nếu không timeline tự khóa ở 20s dù video đã là full.
 */
export function resolveTimelineDuration({
  sourceDuration,
  lastSegmentEnd = 0,
  videoTrackEnd = 0,
  previousMediaDuration = 0,
  workClipSec = 0,
  videoSourceStart = 0,
}: {
  sourceDuration: number
  lastSegmentEnd?: number
  videoTrackEnd?: number
  previousMediaDuration?: number
  workClipSec?: number
  videoSourceStart?: number
}): number {
  const sourceWindow = Math.max(0, sourceDuration - videoSourceStart)
  const windowCap = workClipSec > 0
    ? Math.max(0, Math.min(workClipSec - videoSourceStart, sourceWindow || workClipSec - videoSourceStart))
    : 0
  const available = windowCap > 0
    ? Math.min(Math.max(sourceWindow, lastSegmentEnd, 1), windowCap)
    : Math.max(sourceWindow, lastSegmentEnd, 1)
  const defaultClipAtPreviousEdge = previousMediaDuration > 0
    && Math.abs(videoTrackEnd - previousMediaDuration) <= 0.51
  const sourceExpanded = available > previousMediaDuration + 0.25
  if (videoTrackEnd > 0 && defaultClipAtPreviousEdge && sourceExpanded) return available
  return videoTrackEnd > 0 ? Math.min(videoTrackEnd, available) : available
}

/**
 * Clamp media clips trong cửa sổ làm việc.
 * Không kéo clip đã trim right/left về full span.
 * Chỉ stretch khi cửa sổ phình (preview N→full) và clip từng chạm mép duration cũ.
 */
export function normalizeMediaClips(clips: MediaClip[], durationSec: number, prevDuration = 0): MediaClip[] {
  if (!(durationSec > 0)) return []
  let next = clips
    .filter((c) => c && typeof c.start === 'number' && typeof c.end === 'number' && c.end > c.start)
    .map((c) => ({
      ...c,
      id: String(c.id || crypto.randomUUID()),
      start: Math.max(0, Math.min(c.start, durationSec - MIN_CLIP_SEC)),
      end: Math.max(MIN_CLIP_SEC, Math.min(c.end, durationSec)),
    } as MediaClip))
    .filter((c) => c.end - c.start >= SPLIT_EDGE)
    .sort((a, b) => a.start - b.start || a.end - b.end)
  if (!next.length) return [fullMediaClip(durationSec)]
  // Cửa sổ phình (15s→full / 5s→10s): chỉ kéo clip đã chạm mép cũ — không undo trim right
  if (prevDuration > 0 && durationSec > prevDuration + 0.25) {
    next = next.map((c) => {
      if (Math.abs(c.end - prevDuration) <= 0.51) {
        return { ...c, end: durationSec }
      }
      return c
    })
  }
  return next
}

export function mediaClipsKey(projectId: string, kind: 'video' | 'bg') {
  return `zm_ai_tool.${kind}Clips.${projectId}`
}

export function loadMediaClips(projectId: string, kind: 'video' | 'bg', durationSec: number): MediaClip[] {
  try {
    const raw = localStorage.getItem(mediaClipsKey(projectId, kind))
    if (raw) {
      const parsed = JSON.parse(raw) as MediaClip[]
      if (Array.isArray(parsed) && parsed.length) {
        return normalizeMediaClips(parsed, durationSec)
      }
    }
  } catch { /* ignore */ }
  return durationSec > 0 ? [fullMediaClip(durationSec)] : []
}

export function persistMediaClips(projectId: string, kind: 'video' | 'bg', clips: MediaClip[]) {
  // ponytail: skip [] so projectId reset không ghi đè clip đã lưu
  if (!clips.length) return
  try {
    localStorage.setItem(mediaClipsKey(projectId, kind), JSON.stringify(clips))
  } catch { /* ignore */ }
}

export function splitMediaList(clips: MediaClip[], clipId: string, t: number): MediaClip[] {
  return clips.flatMap((c) => {
    if (c.id !== clipId) return [c]
    if (!(t > c.start + SPLIT_EDGE && t < c.end - SPLIT_EDGE)) return [c]
    return [
      { ...c, end: t },
      { ...c, id: crypto.randomUUID(), start: t, end: c.end, sourceStart: (c.sourceStart ?? c.start) + t - c.start },
    ]
  })
}

export function clipAtTime(clips: MediaClip[], t: number): MediaClip | null {
  return clips.find((c) => t >= c.start && t < c.end) ?? clips.find((c) => t >= c.start && t <= c.end) ?? null
}

/** Remove caption/TTS content covered by a right trim on the Video lane. */
export function trimSegmentsForVideoRight(
  segments: Segment[],
  trimEnd: number,
  previousEnd: number,
  trimsTimelineTail: boolean,
): Segment[] {
  const cutStart = Math.max(0, trimEnd)
  const cutEnd = trimsTimelineTail ? Number.POSITIVE_INFINITY : Math.max(cutStart, previousEnd)
  return segments.flatMap((seg) => {
    if (seg.end <= cutStart || seg.start >= cutEnd) return [seg]
    if (seg.start >= cutStart) return []
    const end = Math.min(seg.end, cutStart)
    if (end - seg.start < 0.04) return []
    const next: Segment = { ...seg, end, captionLayout: null }
    if (typeof seg.coverEnd === 'number') next.coverEnd = Math.min(seg.coverEnd, cutStart)
    if (
      typeof next.coverStart === 'number'
      && typeof next.coverEnd === 'number'
      && next.coverStart > next.coverEnd
    ) {
      next.coverStart = next.coverEnd
    }
    if (seg.isCompound && seg.compoundChildren?.length) {
      const oldSpan = Math.max(0.04, seg.end - seg.start)
      const nextSpan = Math.max(0, end - seg.start)
      next.compoundChildren = trimSegmentsForVideoRight(
        seg.compoundChildren,
        nextSpan,
        oldSpan,
        true,
      )
      if (!next.compoundChildren.length) return []
    }
    return [next]
  })
}

export function __checkTrimSegmentsForVideoRight() {
  const seg = (id: string, start: number, end: number): Segment => ({
    id, index: 0, start, end, source: id, translation: id, voice: 'system',
  })
  const tail = trimSegmentsForVideoRight(
    [seg('before', 1, 3), seg('cross', 4, 7), seg('after', 7, 9)],
    5,
    10,
    true,
  )
  if (tail.map((item) => item.id).join(',') !== 'before,cross' || tail[1]?.end !== 5) {
    throw new Error('trim right tail must clip the crossing caption and remove later caption/TTS')
  }
  const middle = trimSegmentsForVideoRight([seg('cut', 6, 8), seg('later', 11, 12)], 6, 10, false)
  if (middle.length !== 1 || middle[0]?.id !== 'later') {
    throw new Error('trim right middle clip must preserve captions belonging to the later video clip')
  }
  // trim right must survive normalize (single clip from 0)
  const trimmed = normalizeMediaClips([{ id: 'v', start: 0, end: 12, sourceStart: 0 }], 60, 60)
  if (trimmed.length !== 1 || Math.abs(trimmed[0].end - 12) > 0.01) {
    throw new Error(`normalize must keep trim-right end=12, got ${trimmed[0]?.end}`)
  }
  // window expand still stretches clip that touched old edge
  const grown = normalizeMediaClips([{ id: 'v', start: 0, end: 15, sourceStart: 0 }], 60, 15)
  if (grown.length !== 1 || Math.abs(grown[0].end - 60) > 0.01) {
    throw new Error(`normalize must stretch edge-touching clip 15→60, got ${grown[0]?.end}`)
  }
  return true
}

/** Gộp khoảng [a,b) đã sort — dùng ripple delete. */
export function mergeTimeRanges(ranges: { start: number; end: number }[]): { start: number; end: number }[] {
  const sorted = ranges
    .filter((r) => r.end > r.start + 1e-6)
    .slice()
    .sort((a, b) => a.start - b.start)
  if (!sorted.length) return []
  const out: { start: number; end: number }[] = [{ ...sorted[0] }]
  for (let i = 1; i < sorted.length; i++) {
    const cur = sorted[i]
    const last = out[out.length - 1]
    if (cur.start <= last.end + 1e-4) last.end = Math.max(last.end, cur.end)
    else out.push({ ...cur })
  }
  return out
}

/** Tổng thời lượng bị xóa trước mốc t (để shift về 0). */
export function removedBefore(t: number, removed: { start: number; end: number }[]): number {
  let d = 0
  for (const r of removed) {
    if (r.end <= t) d += r.end - r.start
    else if (r.start < t) d += t - r.start
  }
  return d
}

/** Map mốc thời gian sau ripple — điểm nằm trong vùng xóa → mép trái vùng đó. */
export function mapTimeAfterRipple(t: number, removed: { start: number; end: number }[]): number {
  for (const r of removed) {
    if (t >= r.start && t < r.end) return Math.max(0, r.start - removedBefore(r.start, removed))
  }
  return Math.max(0, t - removedBefore(t, removed))
}

/** Xóa clip media + đóng gap (CapCut ripple): kéo phần sau về trước. */
export function rippleDeleteMediaClips(
  clips: MediaClip[],
  dropIds: Set<string>,
): { next: MediaClip[]; removed: { start: number; end: number }[] } {
  const removed = mergeTimeRanges(
    clips.filter((c) => dropIds.has(c.id)).map((c) => ({ start: c.start, end: c.end })),
  )
  if (!removed.length) {
    return { next: clips.filter((c) => !dropIds.has(c.id)), removed: [] }
  }
  const kept = clips
    .filter((c) => !dropIds.has(c.id))
    .map((c) => {
      const start = mapTimeAfterRipple(c.start, removed)
      const end = mapTimeAfterRipple(c.end, removed)
      return { ...c, start, end: Math.max(start + MIN_CLIP_SEC, end) }
    })
    .filter((c) => c.end - c.start >= SPLIT_EDGE)
    .sort((a, b) => a.start - b.start)
  return { next: kept, removed }
}

/** Shift segment/overlay theo vùng đã xóa (ripple toàn project). */
export function rippleShiftSegment(seg: Segment, removed: { start: number; end: number }[]): Segment | null {
  const start = mapTimeAfterRipple(seg.start, removed)
  const end = mapTimeAfterRipple(seg.end, removed)
  if (end - start < 0.04) return null
  const next: Segment = { ...seg, start, end }
  if (typeof seg.coverStart === 'number') {
    next.coverStart = mapTimeAfterRipple(seg.coverStart, removed)
  }
  if (typeof seg.coverEnd === 'number') {
    next.coverEnd = mapTimeAfterRipple(seg.coverEnd, removed)
  }
  if (seg.isCompound && seg.compoundChildren?.length) {
    // Children relative — chỉ scale nếu shell absolute times đổi span
    const oldSpan = Math.max(0.05, seg.end - seg.start)
    const newSpan = Math.max(0.05, end - start)
    const ratio = newSpan / oldSpan
    if (Math.abs(ratio - 1) > 1e-6) {
      next.compoundChildren = seg.compoundChildren.map((ch) => ({
        ...ch,
        start: (Number(ch.start) || 0) * ratio,
        end: (Number(ch.end) || 0) * ratio,
        coverStart:
          typeof ch.coverStart === 'number' ? ch.coverStart * ratio : undefined,
        coverEnd: typeof ch.coverEnd === 'number' ? ch.coverEnd * ratio : undefined,
      }))
    }
  }
  return next
}

export function rippleShiftOverlay(
  ov: TextOverlay,
  removed: { start: number; end: number }[],
): TextOverlay | null {
  const start = mapTimeAfterRipple(ov.start, removed)
  const end = mapTimeAfterRipple(ov.end, removed)
  if (end - start < 0.04) return null
  return { ...ov, start, end }
}
