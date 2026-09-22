import type { Segment } from '@/features/project/project.types'
import type { MediaClip } from './mediaClips'
import { segmentHasDub } from './segmentQuery'

/** TTS manual speed từng câu (không gồm bake global). */
export function dubManualSpeed(seg: Segment): number {
  return Math.max(0.75, Math.min(1.5, seg.ttsSpeed ?? 1))
}

/**
 * playbackRate TTS thật khi preview.
 * TTS tự nhiên trên clock đã fit (seg.ttsBake = bake lúc Lồng tiếng);
 * chỉ scale theo bake/ttsBake khi user đổi tốc độ SAU khi dub.
 * Segment cũ không có ttsBake → mặc định 1 (giữ hành vi ×bake cũ).
 */
export function dubPlaybackSpeed(seg: Segment, bakedSpeed = 1): number {
  const bake =
    typeof bakedSpeed === 'number' && bakedSpeed > 0.2
      ? Math.max(0.5, Math.min(2, bakedSpeed))
      : 1
  const fit =
    typeof seg.ttsBake === 'number' && seg.ttsBake > 0.2 && seg.ttsBake <= 2.5
      ? seg.ttsBake
      : 1
  // Keep the browser clock identical to mux_audio._tts_clip_plan: after a
  // later file bake, the backend permits up to 4× so it never silently cuts
  // the tail of a generated sentence.
  return Math.max(0.5, Math.min(4, dubManualSpeed(seg) * (bake / fit)))
}

/**
 * Playback rate preview — khớp thước + xuất.
 * Thước timeline bất khả xâm phạm: videoSpeed <1 (TTS-fit cũ) đã khai tử —
 * TTS nén bằng atempo phía BE. Chỉ tôn trọng tốc độ clip user đặt (menu ≥1).
 * Muốn 0.80× toàn cục: bấm Áp dụng (bake vào file).
 */
export function previewVideoRate(
  _matchDuration: string | undefined,
  _bakedPreferVideo?: boolean,
  segSpeed?: number,
  _bakedSpeed?: number,
  _hasBakedSpeed?: boolean,
): number {
  const vs =
    typeof segSpeed === 'number' && segSpeed >= 0.995
      ? Math.max(1, Math.min(2, segSpeed))
      : 1
  return vs
}

/** Tốc độ bake file thật (1 = file 1×). Soft preferVideo không tính. */
export function fileBakedSpeed(
  bakedSpeed?: number,
  _bakedPreferVideo?: boolean,
  hasBakedSpeed?: boolean,
): number {
  if (typeof bakedSpeed === 'number' && bakedSpeed > 0.2 && (hasBakedSpeed || Math.abs(bakedSpeed - 1) > 0.02)) {
    return Math.max(0.5, Math.min(2, bakedSpeed))
  }
  return 1
}

/** Slider = tốc độ file đã áp dụng (1.00× nếu chưa bake). Không giả soft 0.8. */
export function displaySpeedDraft(
  _matchDuration: string | undefined,
  bakedSpeed?: number,
  bakedPreferVideo?: boolean,
  hasBakedSpeed?: boolean,
): number {
  return fileBakedSpeed(bakedSpeed, bakedPreferVideo, hasBakedSpeed)
}

/** Format 0.80× / 1.00× / 1.15× */
export function formatSpeedX(speed: number): string {
  const v = Math.round(Math.max(0.5, Math.min(2, speed)) * 100) / 100
  return `${v.toFixed(2)}×`
}

/** Tốc độ file trên đĩa sau Áp dụng (1.00 nếu chưa). */
export function appliedFileSpeed(
  bakedSpeed?: number,
  bakedPreferVideo?: boolean,
  hasBakedSpeed?: boolean,
): number {
  return fileBakedSpeed(bakedSpeed, bakedPreferVideo, hasBakedSpeed)
}

/**
 * Nhãn rõ: đầu vào (file) vs đã áp dụng vs xuất = thước.
 */
export function speedStatusLines(
  matchDuration: string | undefined,
  draftSpeed: number,
  bakedSpeed?: number,
  bakedPreferVideo?: boolean,
  hasBakedSpeed?: boolean,
  locale: 'vi' | 'en' = 'vi',
): { inputLine: string; appliedLine: string; exportLine: string; matchLabel: string } {
  const draft = Math.round(Math.max(0.5, Math.min(2, draftSpeed)) * 100) / 100
  const applied = appliedFileSpeed(bakedSpeed, bakedPreferVideo, hasBakedSpeed)
  const locked =
    Boolean(hasBakedSpeed)
    || Boolean(bakedPreferVideo)
    || Math.abs(applied - 1) > 0.02

  if (locale === 'en') {
    return {
      inputLine: `Input (playing file): ${formatSpeedX(applied)}`,
      appliedLine: locked ? `Applied to all: ${formatSpeedX(applied)} (file + timeline + export)` : `Not applied yet — select ${formatSpeedX(draft)} and click Apply`,
      exportLine: locked ? `Export = timeline at file speed ${formatSpeedX(applied)}` : `Export = timeline at 1.00× (click Apply ${formatSpeedX(draft)} to change speed)`,
      matchLabel: `Duration matching: ${matchDuration || 'settings'} · file ${formatSpeedX(applied)}`,
    }
  }

  let matchLabel = 'Khớp: theo cài đặt'
  if (matchDuration === 'preferVideo') {
    matchLabel = locked
      ? `Khớp preferVideo · file ${formatSpeedX(applied)}`
      : 'Khớp preferVideo · file 1.00× (chưa Áp dụng tốc độ)'
  } else if (matchDuration === 'stretch') matchLabel = 'Khớp: kéo TTS · file theo Áp dụng'
  else if (matchDuration === 'natural') matchLabel = 'Khớp: tự nhiên · file theo Áp dụng'
  else if (matchDuration === 'none') matchLabel = 'Khớp: không · file theo Áp dụng'

  const inputLine = locked
    ? `Đầu vào (file đang phát): ${formatSpeedX(applied)}`
    : 'Đầu vào (file đang phát): 1.00×'

  const appliedLine = locked
    ? `Đã áp dụng cho tất cả: ${formatSpeedX(applied)} (file + timeline + xuất)`
    : Math.abs(draft - 1) > 0.02
      ? `Đã áp dụng cho tất cả: chưa — chọn ${formatSpeedX(draft)}, bấm Áp dụng`
      : 'Đã áp dụng cho tất cả: chưa — file 1.00×'

  const exportLine = locked
    ? `Xuất = thước timeline @ file ${formatSpeedX(applied)}`
    : `Xuất = thước timeline @ 1.00× (bấm Áp dụng ${formatSpeedX(draft)} nếu muốn chậm/nhanh)`

  return { inputLine, appliedLine, exportLine, matchLabel }
}

/** Scale media clip list theo bake speed (Video / Âm gốc local). */
export function scaleMediaClips(list: MediaClip[], scale: number): MediaClip[] {
  if (!list.length || Math.abs(scale - 1) < 1e-9) return list
  return list.map((c) => ({
    ...c,
    start: Math.max(0, c.start * scale),
    end: Math.max(0.05, c.end * scale),
  }))
}

function _roundUs(t: number): number {
  return Math.round(t * 1_000_000) / 1_000_000
}

/** Quy clip display → mốc 1× (baseline bất biến). */
export function mediaClipsTo1xBaseline(list: MediaClip[], currentSpeed: number): MediaClip[] {
  const s = Math.max(0.5, Math.min(2, currentSpeed || 1))
  if (!list.length || Math.abs(s - 1) < 1e-9) {
    return list.map((c) => ({ ...c, start: _roundUs(c.start), end: _roundUs(c.end) }))
  }
  return list.map((c) => ({
    ...c,
    start: _roundUs(c.start * s),
    end: _roundUs(Math.max(0.05, c.end * s)),
    // sourceStart giữ toạ độ file nguồn 1×
  }))
}

/** Từ baseline 1× → display @ speed (mọi lần từ cùng gốc — không cascade). */
export function mediaClipsFrom1xBaseline(baseline: MediaClip[], speed: number): MediaClip[] {
  const s = Math.max(0.5, Math.min(2, speed || 1))
  const scale = 1 / s
  if (!baseline.length) return []
  if (Math.abs(scale - 1) < 1e-9) {
    return baseline.map((c) => ({ ...c, start: _roundUs(c.start), end: _roundUs(c.end) }))
  }
  return baseline.map((c) => ({
    ...c,
    start: _roundUs(c.start * scale),
    end: _roundUs(Math.max(0.05, c.end * scale)),
  }))
}

/**
 * Media-time cần để phát hết TTS khi video chạy `videoRate`.
 * wall = ad / (ttsManual * bake); media = wall * videoRate.
 * bake≠1 → TTS thật nhanh/chậm + clip timeline co giãn khớp caption.
 */
export function dubAudioAbsEnd(
  seg: Segment,
  _segments: Segment[],
  videoRate = 1,
  bakedSpeed = 1,
): number {
  const ttsSpeed = dubPlaybackSpeed(seg, bakedSpeed)
  const ad = seg.audioDuration ?? 0
  const rate = Math.max(0.2, videoRate)
  if (ad > 0.05) {
    return seg.start + (ad / Math.max(0.5, ttsSpeed)) * rate + 0.04
  }
  return Math.max(seg.end, seg.start + 0.05)
}

/** Segment TTS dưới playhead — bỏ qua id đã đọc xong (tránh lặp). */
export function segmentForDub(
  segments: Segment[],
  time: number,
  videoRate = 1,
  finishedIds?: Set<string>,
  bakedSpeed = 1,
  cascadePlan?: Map<string, CascadeInfo>,
): Segment | null {
  let best: Segment | null = null
  for (const s of segments) {
    if (!segmentHasDub(s) || !s.audioUrl) continue
    if (finishedIds?.has(s.id)) continue
    
    const info = cascadePlan?.get(s.id)
    const effectiveStart = info?.effectiveStart ?? s.start
    const effectiveEnd = info?.effectiveEnd ?? dubAudioAbsEnd(s, segments, videoRate, bakedSpeed)

    if (time + 0.03 < effectiveStart) continue
    if (time >= effectiveEnd) continue
    
    const bestStart = best ? (cascadePlan?.get(best.id)?.effectiveStart ?? best.start) : 0
    if (
      !best
      || Math.abs(effectiveStart - time) < Math.abs(bestStart - time)
      || (Math.abs(effectiveStart - time) === Math.abs(bestStart - time) && effectiveStart > bestStart)
    ) {
      best = s
    }
  }
  return best
}

/** Chiều rộng clip TTS trên timeline (giây media) */
export function dubClipSeconds(
  seg: Segment,
  segments: Segment[],
  videoRate = 1,
  bakedSpeed = 1,
): number {
  return Math.max(0.05, dubAudioAbsEnd(seg, segments, videoRate, bakedSpeed) - seg.start)
}

export interface CascadeInfo {
  effectiveStart: number
  effectiveEnd: number
}

/**
 * Tính toán timeline thực tế khi các câu bị đẩy lùi (cascade).
 * Mô phỏng lại logic `mux_audio.py` ở backend.
 */
export function buildCascadePlan(
  segments: Segment[],
  bakedSpeed = 1,
): Map<string, CascadeInfo> {
  const plan = new Map<string, CascadeInfo>()
  let cursor = 0

  const sorted = [...segments]
    .filter((s) => segmentHasDub(s) && s.audioUrl)
    .sort((a, b) => a.start - b.start)

  for (let i = 0; i < sorted.length; i += 1) {
    const seg = sorted[i]
    const start = Math.max(seg.start, cursor)
    const ttsSpeed = dubPlaybackSpeed(seg, bakedSpeed)
    const ad = seg.audioDuration ?? 0
    // Match the export mixer byte-for-byte in time math.  In particular,
    // `trim` includes the 40 ms mixer tail, and the following sentence starts
    // after the same 20 ms gap.  Do not use a videoSpeed here: export retimes
    // the video first, then places this audio plan on that final clock.
    const next = sorted.slice(i + 1).find((candidate) => candidate.start > seg.start + 0.02)
    const slot = next
      ? Math.max(0.12, next.start - seg.start - 0.03)
      : Math.max(0.15, ad > 0.05 ? ad + 0.15 : Math.max(0, seg.end - seg.start) + 0.12)
    const played = ad > 0.05 ? ad / Math.max(0.05, ttsSpeed) : slot
    const trim = Math.max(0.08, played + 0.04)
    const end = start + trim
    plan.set(seg.id, { effectiveStart: start, effectiveEnd: end })
    cursor = end + 0.02
  }

  return plan
}
