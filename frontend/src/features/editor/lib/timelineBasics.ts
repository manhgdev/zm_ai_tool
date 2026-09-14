import type { Segment } from '@/features/project/project.types'

export function formatTime(value: number) {
  // Làm tròn TRƯỚC khi tách phút/giây: 119.97 từng ra "1:60.0" thay vì "2:00.0"
  const tenths = Math.round(Math.max(0, value) * 10)
  const min = Math.floor(tenths / 600)
  const sec = (tenths % 600) / 10
  return `${min}:${sec.toFixed(1).padStart(4, '0')}`
}

export const BOOKMARK_EPS = 1 / 30
export const MIN_CLIP_SEC = 0.15
/** Lề tối thiểu hai phía để còn cắt được (clip ngắn ~0.4s vẫn split được) */
export const SPLIT_EDGE = 0.05
/** Zoom rất nhỏ cho video dài — không kẹp 0.05 (sẽ full ngang). */
export const ZOOM_MIN = 0.002
export const ZOOM_MAX = 40
export const PX_PER_SEC_BASE = 50

/** Fit / kéo hết cỡ trái = nội dung chiếm ~80% khung. */
export const FIT_WIDTH_RATIO = 0.8

export function fitTimelineZoom(durationSec: number, widthPx: number, widthRatio = FIT_WIDTH_RATIO) {
  if (durationSec <= 0 || widthPx <= 0) return 1
  const usable = Math.max(48, (widthPx - 8) * widthRatio)
  const z = usable / (durationSec * PX_PER_SEC_BASE)
  // Không kẹp ZOOM_MIN cao — video dài cần z << 0.05 để còn 50% trống
  return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round(z * 10000) / 10000))
}

export function bookmarkKey(projectId: string) {
  return `zm_ai_tool.bookmarks.${projectId}`
}

export function loadBookmarks(projectId: string): number[] {
  try {
    const raw = localStorage.getItem(bookmarkKey(projectId))
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed.filter((t): t is number => typeof t === 'number' && Number.isFinite(t)).sort((a, b) => a - b)
  } catch {
    return []
  }
}

export function persistBookmarks(projectId: string, marks: number[]) {
  try {
    localStorage.setItem(bookmarkKey(projectId), JSON.stringify(marks))
  } catch {
    /* ignore */
  }
}

export function reindexSegments(list: Segment[]): Segment[] {
  return [...list]
    .sort((a, b) => a.start - b.start || a.end - b.end)
    .map((s, i) => ({ ...s, index: i }))
}
