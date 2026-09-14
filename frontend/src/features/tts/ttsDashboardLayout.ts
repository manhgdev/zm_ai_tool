/** TTS overview dashboard: fine grid, free resize (push peers), move, no overlap. */

import type { CSSProperties } from 'react'

export const TTS_DASH_LAYOUT_KEY = 'zm-ai-tool:tts-dash-layout:v7'
export const DASH_COLS = 24
export const DASH_ROWS = 4
export const DASH_MIN_W = 3
export const DASH_MIN_H = 1

export const DASH_IDS = ['input', 'voice', 'advanced', 'clone', 'preview', 'export'] as const
export type DashId = (typeof DASH_IDS)[number]

export type DashItem = { id: DashId; col: number; row: number; w: number; h: number }
export type DashLayout = Record<DashId, DashItem>
export type ResizeHandle = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'

/** Default: 4 top + preview | export side-by-side (như UI cũ). */
export const DEFAULT_DASH_LAYOUT: DashLayout = {
  input: { id: 'input', col: 0, row: 0, w: 6, h: 2 },
  voice: { id: 'voice', col: 6, row: 0, w: 6, h: 2 },
  advanced: { id: 'advanced', col: 12, row: 0, w: 6, h: 2 },
  clone: { id: 'clone', col: 18, row: 0, w: 6, h: 2 },
  preview: { id: 'preview', col: 0, row: 2, w: 16, h: 2 },
  export: { id: 'export', col: 16, row: 2, w: 8, h: 2 },
}

function clamp(n: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, n))
}

export function clampItem(item: DashItem): DashItem {
  const w = clamp(Math.round(item.w), DASH_MIN_W, DASH_COLS)
  const h = clamp(Math.round(item.h), DASH_MIN_H, DASH_ROWS)
  const col = clamp(Math.round(item.col), 0, DASH_COLS - w)
  const row = clamp(Math.round(item.row), 0, DASH_ROWS - h)
  return { id: item.id, col, row, w, h }
}

export function overlaps(a: DashItem, b: DashItem) {
  return a.col < b.col + b.w && a.col + a.w > b.col && a.row < b.row + b.h && a.row + a.h > b.row
}

function anyOverlap(layout: DashLayout, id: DashId, item: DashItem) {
  return DASH_IDS.some((other) => other !== id && overlaps(item, layout[other]))
}

/** Compress peers that block growth along the active handle; keep min size. */
function pushPeers(
  layout: DashLayout,
  id: DashId,
  item: DashItem,
  handle: ResizeHandle,
): DashLayout | null {
  let next = { ...layout, [id]: clampItem(item) }
  for (let guard = 0; guard < 64; guard++) {
    const me = next[id]
    const hit = DASH_IDS.find((other) => other !== id && overlaps(me, next[other]))
    if (!hit) return next
    const peer = next[hit]
    let p = { ...peer }
    let changed = false

    if (handle.includes('e') && me.col + me.w > peer.col && me.col < peer.col + peer.w) {
      const cut = me.col + me.w - peer.col
      if (cut > 0 && peer.w - cut >= DASH_MIN_W) {
        p = { ...p, col: peer.col + cut, w: peer.w - cut }
        changed = true
      }
    }
    if (handle.includes('w') && me.col < peer.col + peer.w && me.col + me.w > peer.col) {
      const cut = peer.col + peer.w - me.col
      if (cut > 0 && peer.w - cut >= DASH_MIN_W) {
        p = { ...p, w: peer.w - cut }
        changed = true
      }
    }
    if (handle.includes('s') && me.row + me.h > peer.row && me.row < peer.row + peer.h) {
      const cut = me.row + me.h - peer.row
      if (cut > 0 && peer.h - cut >= DASH_MIN_H) {
        p = { ...p, row: peer.row + cut, h: peer.h - cut }
        changed = true
      }
    }
    if (handle.includes('n') && me.row < peer.row + peer.h && me.row + me.h > peer.row) {
      const cut = peer.row + peer.h - me.row
      if (cut > 0 && peer.h - cut >= DASH_MIN_H) {
        p = { ...p, h: peer.h - cut }
        changed = true
      }
    }

    if (!changed) return null
    p = clampItem(p)
    if (overlaps(me, p)) return null
    next = { ...next, [hit]: p }
  }
  return null
}

/** Undo growth toward origin until no overlap (fallback when push fails). */
function shrinkGrownEdges(
  layout: DashLayout,
  id: DashId,
  candidate: DashItem,
  origin: DashItem,
  handle: ResizeHandle,
): DashItem {
  let next = clampItem(candidate)
  for (let guard = 0; guard < 96; guard++) {
    if (!anyOverlap(layout, id, next)) return next
    let changed = false
    if (handle.includes('e') && next.w > origin.w) {
      next = { ...next, w: next.w - 1 }
      changed = true
    } else if (handle.includes('w') && next.w > origin.w) {
      next = { ...next, col: next.col + 1, w: next.w - 1 }
      changed = true
    } else if (handle.includes('s') && next.h > origin.h) {
      next = { ...next, h: next.h - 1 }
      changed = true
    } else if (handle.includes('n') && next.h > origin.h) {
      next = { ...next, row: next.row + 1, h: next.h - 1 }
      changed = true
    }
    if (!changed) return clampItem(origin)
    next = clampItem(next)
  }
  return clampItem(origin)
}

export function parseDashLayout(raw: unknown): DashLayout {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return structuredClone(DEFAULT_DASH_LAYOUT)
  const src = raw as Record<string, unknown>
  const sample = src.input
  if (sample && typeof sample === 'object' && 'x' in (sample as object) && !('col' in (sample as object))) {
    return structuredClone(DEFAULT_DASH_LAYOUT)
  }
  const next = structuredClone(DEFAULT_DASH_LAYOUT)
  for (const id of DASH_IDS) {
    const v = src[id]
    if (!v || typeof v !== 'object') continue
    const o = v as Record<string, unknown>
    next[id] = clampItem({
      id,
      col: Number(o.col),
      row: Number(o.row),
      w: Number(o.w),
      h: Number(o.h),
    })
  }
  return next
}

export function loadDashLayout(): DashLayout {
  try {
    const raw = localStorage.getItem(TTS_DASH_LAYOUT_KEY)
    if (!raw) return structuredClone(DEFAULT_DASH_LAYOUT)
    return parseDashLayout(JSON.parse(raw))
  } catch {
    return structuredClone(DEFAULT_DASH_LAYOUT)
  }
}

export function persistDashLayout(layout: DashLayout) {
  try {
    localStorage.setItem(TTS_DASH_LAYOUT_KEY, JSON.stringify(layout))
  } catch {
    /* ignore */
  }
}

export function itemStyle(item: DashItem): CSSProperties {
  const c = clampItem(item)
  return {
    gridColumn: `${c.col + 1} / span ${c.w}`,
    gridRow: `${c.row + 1} / span ${c.h}`,
  }
}

/** Resize from origin + cell deltas; push peers when possible, else stop at edge. */
export function resizeFromHandle(
  layout: DashLayout,
  id: DashId,
  origin: DashItem,
  handle: ResizeHandle,
  dx: number,
  dy: number,
): DashLayout {
  let col = origin.col
  let row = origin.row
  let w = origin.w
  let h = origin.h

  if (handle.includes('e')) w = origin.w + dx
  if (handle.includes('w')) {
    col = origin.col + dx
    w = origin.w - dx
  }
  if (handle.includes('s')) h = origin.h + dy
  if (handle.includes('n')) {
    row = origin.row + dy
    h = origin.h - dy
  }

  if (w < DASH_MIN_W) {
    if (handle.includes('w')) col = origin.col + origin.w - DASH_MIN_W
    w = DASH_MIN_W
  }
  if (h < DASH_MIN_H) {
    if (handle.includes('n')) row = origin.row + origin.h - DASH_MIN_H
    h = DASH_MIN_H
  }

  const candidate = clampItem({ id, col, row, w, h })
  if (!anyOverlap(layout, id, candidate)) return { ...layout, [id]: candidate }

  const pushed = pushPeers(layout, id, candidate, handle)
  if (pushed) return pushed

  const next = shrinkGrownEdges(layout, id, candidate, origin, handle)
  return { ...layout, [id]: next }
}

/** Move by cell delta from origin; swap with peer if would overlap. */
export function moveDashItem(
  layout: DashLayout,
  id: DashId,
  origin: DashItem,
  dCol: number,
  dRow: number,
): DashLayout {
  const tentative = clampItem({ ...origin, col: origin.col + dCol, row: origin.row + dRow })
  const hit = DASH_IDS.find((other) => other !== id && overlaps(tentative, layout[other]))
  if (!hit) return { ...layout, [id]: tentative }
  const peer = layout[hit]
  const a = clampItem({ ...origin, col: peer.col, row: peer.row })
  const b = clampItem({ ...peer, col: origin.col, row: origin.row })
  const trial = { ...layout, [id]: a, [hit]: b }
  if (anyOverlap(trial, id, a) || anyOverlap(trial, hit, b)) return layout
  return trial
}

export function __checkDashLayout() {
  const a = clampItem({ id: 'input', col: 99, row: -2, w: 1, h: 9 })
  if (a.w !== DASH_MIN_W || a.h !== DASH_ROWS || a.col !== DASH_COLS - DASH_MIN_W || a.row !== 0) {
    throw new Error('clamp failed')
  }

  const west = resizeFromHandle(DEFAULT_DASH_LAYOUT, 'voice', DEFAULT_DASH_LAYOUT.voice, 'w', -1, 0)
  if (anyOverlap(west, 'voice', west.voice)) throw new Error('west overlap')
  // Growing west should push/shrink input, not stay stuck
  if (west.voice.col >= DEFAULT_DASH_LAYOUT.voice.col) throw new Error('west should grow left')

  const room = {
    ...DEFAULT_DASH_LAYOUT,
    voice: { id: 'voice' as const, col: 10, row: 0, w: 4, h: 2 },
    advanced: { id: 'advanced' as const, col: 16, row: 0, w: 4, h: 2 },
    clone: { id: 'clone' as const, col: 20, row: 0, w: 4, h: 2 },
  }
  const grown = resizeFromHandle(room, 'input', room.input, 'e', 2, 0)
  if (grown.input.w < room.input.w + 2) throw new Error('east grow into gap failed')

  const swapped = moveDashItem(DEFAULT_DASH_LAYOUT, 'input', DEFAULT_DASH_LAYOUT.input, 6, 0)
  if (swapped.input.col !== 6 || swapped.voice.col !== 0) throw new Error('swap move failed')
}
