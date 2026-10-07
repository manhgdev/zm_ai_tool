import type { ProjectSettings, Segment } from '@/features/project/project.types'
import { speakerTextColor } from '@/features/project/speakerProfiles'
import {
  OCR_MID_PAD_EM,
  layoutOcrOverlay,
} from '@/features/editor/ocrOverlayLayout'
import { resolveCropRect, captionFontCss, type PixelBox, type CropRect } from './previewStyles'
import { effectiveOverlayLayout } from './segmentQuery'
import {
  CAP_PAD_X,
  fitCaptionLines,
  lineNeedWidth,
  measureLineWidth,
  measureSourceInkWidth,
  setMeasureFontFamily,
  wrapCaptionText,
} from './captionMeasure'
import {
  AUTO_SUBTITLE_FONT,
  COVER_SHADOW_BOT,
  autoFontFromBbox,
  captionCenterInCover,
  captionPlacement,
  clampCoverBox,
  coverBoxWidth,
  coverContentWidth,
  coverInnerWidth,
  coverPad,
  coverToAnchor,
  cropCoversFull,
  expandCoverCentered,
  expandCoverForCaptionLines,
  fallbackCoverBox,
  fitHardsubCover,
  frameMaxInnerWidth,
  intersectBox,
  normalizeCoverBox,
  resolveCaptionFontSize,
  resolveInkWidth,
  resolveOverlayFontPreferred,
  seedCoverBox,
  tightenStoredBbox,
  unionBox,
} from './coverBox'

export type OverLayout = { cover: PixelBox; caption: PixelBox; lines: string[]; fontPx?: number }

/** Layout over: cover full ngang nếu cần; 1 dòng (co font) rồi mới 2 dòng. */
export function layoutOverMode(
  anchor: PixelBox,
  text: string,
  fontSizePx: number,
  frameW: number,
  frameH: number,
  sourceText = '',
  inkW?: number,
): OverLayout {
  const pad = coverPad(fontSizePx, frameW)
  const cx = anchor.x + anchor.w / 2
  const trimmed = text.trim()
  // Full frame inner — xếp chữ Việt full ngang
  const maxInnerW = frameMaxInnerWidth(fontSizePx, frameW)
  const { lines, fontPx } = fitCaptionLines(trimmed, maxInnerW, fontSizePx, {
    preferOneLine: true,
    maxLines: 2,
  })

  const lineH = fontPx * 1.12
  const textBlockH = Math.ceil(lines.length * lineH + 4)
  const textW = Math.max(...lines.map((l) => measureLineWidth(l, fontPx)), 1)

  const sourceTrim = sourceText.trim()
  const sourceW = sourceTrim ? measureSourceInkWidth(sourceTrim, fontPx, anchor.h) : 0
  const origW = inkW ?? (sourceTrim ? Math.max(sourceW, anchor.w) : anchor.w)
  const contentW = coverContentWidth(origW, textW)
  const inkCapW = Math.max(...lines.map((l) => lineNeedWidth(l, fontPx)), Math.ceil(textW + CAP_PAD_X * 2))
  // Cover: max(OCR, chữ VI) — căn tâm OCR
  const coverW = Math.min(frameW, Math.max(coverBoxWidth(contentW, frameW), inkCapW + pad.x * 2))
  const coverX = Math.round(Math.max(0, Math.min(frameW - coverW, cx - coverW / 2)))
  const coverY = Math.max(0, anchor.y - pad.top)
  const coverH = Math.min(
    frameH - coverY,
    Math.max(anchor.h, textBlockH) + pad.top + pad.bottom + COVER_SHADOW_BOT,
  )
  // 1 dòng: caption full cover (bbox vàng không hẹp lệch); 2 dòng: theo dòng dài
  const edge = Math.max(2, Math.round(coverW * 0.01))
  const captionW = Math.ceil(
    lines.length === 1 ? Math.max(inkCapW, coverW - edge * 2) : inkCapW,
  )
  const captionX = Math.round(Math.max(coverX, Math.min(coverX + coverW - captionW, coverX + coverW / 2 - captionW / 2)))
  const captionY = captionCenterInCover(coverY, coverH, textBlockH)

  return {
    cover: { x: Math.round(coverX), y: Math.round(coverY), w: Math.round(coverW), h: Math.round(coverH) },
    caption: { x: captionX, y: captionY, w: captionW, h: textBlockH },
    lines,
    fontPx,
  }
}


/** Cover hiển thị / xuất — bbox lưu trực tiếp khung này (mode over). */
export function resolveSegmentCover(
  seg: Segment | undefined,
  settings: ProjectSettings,
  frameW: number,
  frameH: number,
): PixelBox | null {
  if (!seg) return null
  setMeasureFontFamily(captionFontCss(seg.fontFamily || settings.subtitleFontFamily || 'system'))
  const fontPx = resolveCaptionFontSize(seg, settings, frameW, frameH)
  const over = settings.coverHardsubs && settings.burnSubs && seg.translation.trim()
  const overlayLay = effectiveOverlayLayout(seg, frameH, frameW)
  if (overlayLay) {
    return overlayCoverSeed(seg, frameW, frameH)
      ?? (seg.layout === 'horizontal' ? fallbackCoverBox(frameW, frameH, fontPx) : null)
  }
  if (!over) {
    const seed = seg.bbox
      ? tightenStoredBbox(seg, clampCoverBox(seg.bbox, frameW, frameH), frameW)
      : seedCoverBox(seg, frameW, frameH, fontPx)
    if (!seed) return null
    return normalizeCoverBox(seed, frameW, frameH, fontPx)
  }
  if (seg.bbox) {
    return tightenStoredBbox(seg, clampCoverBox(seg.bbox, frameW, frameH), frameW)
  }
  const seed = seedCoverBox(seg, frameW, frameH, fontPx)
  if (!seed) return null
  const anchor = normalizeCoverBox(seed, frameW, frameH, fontPx)
  return fitCoverBoxOver(anchor, seg.translation, fontPx, frameW, frameH, seg.source ?? '')
}

/** Seed khung che overlay: chỉ fallback đúng layout; không bbox CJK → null (đừng bịa cột dọc). */
export function overlayCoverSeed(seg: Segment, frameW: number, frameH: number): PixelBox | null {
  const seed = seg.coverBox ?? seg.bbox
  if (!seed) {
    return null
  }
  const box = clampCoverBox(seed, frameW, frameH)
  // mid: chỉ bỏ khung gần full-frame (lưới đáy nhầm). 2 dòng hardsub giữa/đáy vẫn giữ.
  return box
}

export function isBadOverlayStoredCover(seg: Segment, cover: PixelBox, _frameW = 1080, frameH = 1920): boolean {
  if (seg.layout === 'vertical' && cover.w > cover.h * 0.85) return true
  // Caption đáy full ngang OK; mid/label chỉ chặn H bất thường
  if (seg.layout === 'mid' && cover.h > frameH * 0.28) return true
  if (seg.layout === 'label' && cover.h > frameH * 0.35) return true
  return false
}

export function toCaptionLayout(caption: PixelBox, lines: string[], fontSize: number): NonNullable<Segment['captionLayout']> {
  return { x: caption.x, y: caption.y, w: caption.w, h: caption.h, lines, fontSize }
}

/** User đã kéo tay / lưu layout — giữ nguyên bbox (không adaptive reset). */
export function hasStoredLayout(seg: Segment | undefined, fontPx?: number): boolean {
  const cl = seg?.captionLayout
  const b = seg?.captionBox ?? seg?.bbox
  if (!(b && cl?.lines?.length && cl.w > 0 && cl.h > 0)) return false
  if (fontPx != null && fontPx > 0 && cl.fontSize > 0 && fontPx !== cl.fontSize) return false
  return true
}

/** Đọc đúng bbox + captionLayout đã lưu — không tính lại (preview = xuất). */
export function storedOverLayout(seg: Segment, frameW: number, frameH: number): OverLayout | null {
  const cl = seg.captionLayout
  const b = seg.captionBox ?? seg.bbox
  if (!b || !cl?.lines?.length || cl.w <= 0 || cl.h <= 0) return null
  return {
    cover: clampCoverBox(b, frameW, frameH),
    caption: {
      x: Math.round(cl.x),
      y: Math.round(cl.y),
      w: Math.max(1, Math.round(cl.w)),
      h: Math.max(1, Math.round(cl.h)),
    },
    lines: cl.lines.map(String),
  }
}

/** Chỉ gọi khi chưa có layout lưu hoặc user vừa chỉnh cover/chữ. */
export function resolveOverLayout(
  seg: Segment | undefined,
  settings: ProjectSettings,
  frameW: number,
  frameH: number,
  coverOverride?: PixelBox,
): OverLayout | null {
  const effOverride = coverOverride ?? (seg?.captionBox ? clampCoverBox(seg.captionBox, frameW, frameH) : undefined)
  if (!seg?.translation.trim()) return null
  if (!settings.burnSubs) return null
  // ponytail: sync measurement font with CSS render font — prevents text overflow
  setMeasureFontFamily(captionFontCss(seg.fontFamily || settings.subtitleFontFamily || 'system'))
  const fontPx = resolveCaptionFontSize(seg, settings, frameW, frameH)

  // Overlay OCR mid / dọc / nhãn — hoặc horizontal có bbox giữa khung
  // (không phụ thuộc coverHardsubs: chữ vẫn đúng chỗ; mask mới cần cover)
  const overlayLay = effectiveOverlayLayout(seg, frameH, frameW)
  if (overlayLay) {
    const preferred = resolveOverlayFontPreferred(seg)
    if (effOverride) {
      // Kéo tay hoặc có captionBox riêng: fit theo khung draft/captionBox
      const lockFs = resolveOverlayFontPreferred(seg)
      const laid = layoutOcrOverlay(overlayLay, effOverride, seg.translation, lockFs, frameW, frameH, false)
      return {
        cover: clampCoverBox(effOverride, frameW, frameH),
        caption: laid.caption,
        lines: laid.lines,
        fontPx: laid.fontPx,
      }
    }
    // Persisted captionLayout is authoritative only after an explicit drag.
    // Auto layouts from older cache versions must be recomputed on reopen.
    if (seg.bboxInherited === false && hasStoredLayout(seg, undefined)) {
      const stored = storedOverLayout(seg, frameW, frameH)
      if (stored && !isBadOverlayStoredCover(seg, stored.cover, frameW, frameH)) {
        // Tin bbox/mask đã lưu; chữ xếp lại trong cover (tránh captionLayout x/y lệch → chữ sai chỗ)
        const cover = stored.cover
        // captionLayout.fontSize là kết quả auto cũ, không phải lựa chọn khóa
        // của người dùng. Bỏ nó để bbox dài tự tính lại font lớn nhất có thể.
        // 0 = auto fit bbox; preferred chỉ khi user set fontSize đoạn
        // Mid retries the shared project font and grows its automatic cover;
        // otherwise a stored OCR box permanently keeps long captions tiny.
        const want = preferred > 0 ? preferred : overlayLay === 'mid' ? fontPx : 0
        const laid = layoutOcrOverlay(overlayLay, cover, seg.translation, want, frameW, frameH)
        return {
          cover: clampCoverBox(laid.cover, frameW, frameH),
          caption: laid.caption,
          lines: laid.lines,
          fontPx: laid.fontPx,
        }
      }
    }
    // Caption/CAP-MID share one bbox engine. Caption without OCR keeps only
    // the old bottom coordinate as its fallback; its fitting is still mid.
    const seed = overlayCoverSeed(seg, frameW, frameH)
      ?? (seg.layout === 'horizontal' ? fallbackCoverBox(frameW, frameH, fontPx) : null)
    if (!seed) return null
    // Mid captions use the shared caption font first; bbox fitting may shrink
    // only when that size cannot fit. Vertical/label keep their own auto-fit.
    const want = preferred > 0
      ? preferred
      : overlayLay === 'mid'
        ? fontPx
        : 0
    const laid = layoutOcrOverlay(overlayLay, seed, seg.translation, want, frameW, frameH)
    // CAP-MID/mid: cover tu layoutMidOverlay (trong seed OCR) — khong doi caption day
    return {
      cover: clampCoverBox(laid.cover, frameW, frameH),
      caption: laid.caption,
      lines: laid.lines,
      fontPx: laid.fontPx,
    }
  }

  // Caption đáy/over horizontal — cần chế độ che chữ
  if (!settings.coverHardsubs && !effOverride) return null

  // Đang kéo hoặc có captionBox riêng: bám đúng khung
  if (effOverride) {
    const dragFont = Math.max(
      10,
      Math.floor(autoFontFromBbox(effOverride, seg.translation, fontPx) * 0.86),
    )
    return manualCoverLayout(effOverride, seg.translation, dragFont, frameW, frameH, true, false)
  }

  // Đã lưu từ editor (kéo tay) — giữ đúng bbox; chỉ xếp chữ trong cover (như mid)
  // A dragged bbox stores the fitted font, which is usually smaller than the
  // project default. Do not reject that layout merely because the default
  // font changed; doing so re-runs the path with 48px and overflows the box.
  if (seg.bboxInherited === false && hasStoredLayout(seg, undefined)) {
    const stored = storedOverLayout(seg, frameW, frameH)
    if (!stored) return null
    // Giữ bbox đã kéo; chỉ nới ngang căn tâm nếu chữ tràn (không refit full).
    const fs = Number(seg.captionLayout?.fontSize) > 0
      ? Number(seg.captionLayout?.fontSize)
      : fontPx
    const lines = stored.lines.length ? stored.lines : [seg.translation.trim()]
    const needW = Math.max(...lines.map((l) => lineNeedWidth(l, fs)), 1)
      + coverPad(fs, frameW).x * 2
    const cover = needW > stored.cover.w
      ? expandCoverCentered(stored.cover, needW, frameW, frameH)
      : stored.cover
    if (cover.w === stored.cover.w && cover.x === stored.cover.x) {
      return { ...stored, cover, fontPx: fs }
    }
    const laid = layoutCaptionInCover(cover, seg.translation, fs, frameW)
    return {
      cover,
      caption: laid.caption,
      lines: laid.lines.length ? laid.lines : lines,
      fontPx: laid.fontPx ?? fs,
    }
  }

  const seedRaw = seg.bbox
    ? tightenStoredBbox(seg, clampCoverBox(seg.bbox, frameW, frameH), frameW)
    : seedCoverBox(seg, frameW, frameH, fontPx)
      // Whisper can provide a translated horizontal caption without an OCR bbox.
      // In cover mode, use the same bottom fallback shown by the editor handles so
      // the mask and translated text are rendered instead of silently disappearing.
      ?? fallbackCoverBox(frameW, frameH, fontPx)
  // Bbox OCR is the coverage contract: never crop an edge after detection.
  const seed = normalizeCoverBox(seedRaw, frameW, frameH, fontPx)

  // Bbox OCR / user: cover cố định như mid — fit chữ trong box, không phình sau drag
  const anchor = coverToAnchor(seed, fontPx, frameW)
  if (seg.bbox) {
    if (seg.bboxInherited === false) {
      const fixedFont = Number(seg.captionLayout?.fontSize) > 0
        ? Number(seg.captionLayout?.fontSize)
        : autoFontFromBbox(seed, seg.translation, 0)
      const laid = manualCoverLayout(seed, seg.translation, fixedFont, frameW, frameH, true)
      return { ...laid, fontPx: laid.fontPx ?? fixedFont }
    }
    // Auto OCR boxes are tight around source glyphs; leave room for the
    // translated glyph ascenders/descenders and shadow before first drag.
    // ponytail: keep this margin only on inherited OCR, while user-dragged
    // layouts retain their exact stored fit.
    const autoFontPx = fontPx
    const laid = manualCoverLayout(seed, seg.translation, autoFontPx, frameW, frameH, true)
    return { ...laid, fontPx: autoFontPx }
  }

  const sourceTrim = (seg.source ?? '').trim()
  const sourceW = sourceTrim ? measureSourceInkWidth(sourceTrim, fontPx, anchor.h) : 0
  const inkW = resolveInkWidth(anchor, seed, !!sourceTrim, sourceW, frameW)
  const auto = layoutOverMode(anchor, seg.translation, fontPx, frameW, frameH, seg.source ?? '', inkW)
  const cover = fitHardsubCover(seed, auto.cover.w, fontPx, frameW, frameH, seg.source ?? '')
  const laid = layoutCaptionInCover(cover, seg.translation, fontPx, frameW)
  return { cover, ...laid, fontPx }
}

function fitBoxToCrop(box: PixelBox, crop: CropRect): PixelBox {
  const scale = Math.min(1, crop.w / Math.max(1, box.w), crop.h / Math.max(1, box.h))
  const w = Math.max(4, Math.round(box.w * scale))
  const h = Math.max(4, Math.round(box.h * scale))
  const centerX = box.x + box.w / 2
  const centerY = box.y + box.h / 2
  return {
    x: Math.round(Math.max(crop.x, Math.min(crop.x + crop.w - w, centerX - w / 2))),
    y: Math.round(Math.max(crop.y, Math.min(crop.y + crop.h - h, centerY - h / 2))),
    w,
    h,
  }
}

export type PreviewOverLayout = OverLayout & { mask: PixelBox | null }

/** Mask che chữ gốc — không cần bản dịch (mid/label/dọc OCR). */
export function resolveCoverMaskOnly(
  seg: Segment,
  frameW: number,
  frameH: number,
  crop: CropRect,
  coverOverride?: PixelBox,
): PixelBox | null {
  const seed = coverOverride ?? overlayCoverSeed(seg, frameW, frameH)
  if (!seed) return null
  const cover = clampCoverBox(seed, frameW, frameH)
  if (cropCoversFull(crop, frameW, frameH)) return cover
  const ink = intersectBox(cover, crop) ?? intersectBox(
    seg.bbox ? clampCoverBox(seg.bbox, frameW, frameH) : cover,
    crop,
  )
  return ink
}

/** Preview: cover/caption luôn nằm trong crop hiện tại (16:9, 9:16…). */
export function resolvePreviewOverLayout(
  seg: Segment | undefined,
  settings: ProjectSettings,
  frameW: number,
  frameH: number,
  crop: CropRect,
  coverOverride?: PixelBox,
): PreviewOverLayout | null {
  const effOverride = coverOverride ?? (seg?.captionBox ? clampCoverBox(seg.captionBox, frameW, frameH) : undefined)
  const base = resolveOverLayout(seg, settings, frameW, frameH, effOverride)
  if (!base) return null
  // Text placement and source coverage are independent. A caption drag must
  // never move (or synthesize) the mask, including after saving/reopening.
  const sourceMask = seg ? resolveCoverMaskOnly(seg, frameW, frameH, crop) : null
  // Nếu segment có bbox/captionBox HOẶC thuộc layout mid/label/vertical
  // -> GIỮ NGUYÊN tọa độ đè đúng chỗ. Không tự động shift/fallback xuống đáy màn hình.
  const overlayLay = seg ? effectiveOverlayLayout(seg, frameH, frameW) : null
  if (
    effOverride ||
    overlayLay === 'mid' ||
    overlayLay === 'label' ||
    overlayLay === 'vertical' ||
    seg?.captionBox ||
    seg?.bbox
  ) {
    const finalCover = overlayLay === 'mid' && seg?.bboxInherited !== false && !effOverride
      ? expandCoverForCaptionLines(
          base.cover,
          base.lines.length,
          base.fontPx ?? resolveCaptionFontSize(seg, settings, frameW, frameH),
          frameW,
          frameH,
        )
      : base.cover
    const finalBase = finalCover.h === base.cover.h && finalCover.y === base.cover.y
      ? base
      : {
          ...base,
          cover: finalCover,
          caption: {
            ...base.caption,
            y: finalCover.y,
            h: finalCover.h,
          },
        }
    const fullCrop = cropCoversFull(crop, frameW, frameH)
    const cover = fullCrop ? finalBase.cover : fitBoxToCrop(finalBase.cover, crop)
    const caption = fullCrop ? finalBase.caption : fitBoxToCrop(finalBase.caption, crop)
    return { ...finalBase, cover, caption, mask: sourceMask }
  }

  // Dưới đây là logic dành cho Whisper (dịch giọng nói, KHÔNG CÓ BBOX)
  // -> tự động fallback căn lề dưới cùng của vùng video (crop).
  // Caption đáy 16:9 — logic HEAD gốc (không sửa mid)
  if (crop.w >= crop.h) {
    const fontPx = base.fontPx ?? 16
    const padY = Math.max(2, Math.round(fontPx * 0.08))
    const offsetY = Math.max(3, Math.round(fontPx * 0.25))
    const caption = {
      ...base.caption,
      y: Math.min(crop.y + crop.h - base.caption.h - padY, base.caption.y + offsetY),
    }
    const y = Math.max(crop.y, caption.y - padY)
    const bottom = Math.min(crop.y + crop.h, caption.y + caption.h + padY)
    const cover = { ...base.cover, y, h: Math.max(4, bottom - y) }
    return { ...base, cover, caption, mask: sourceMask }
  }
  const fullCrop = cropCoversFull(crop, frameW, frameH)
  const fittedCover = fullCrop ? base.cover : fitBoxToCrop(base.cover, crop)
  let caption = fullCrop ? base.caption : fitBoxToCrop(base.caption, crop)
  const text = seg?.translation?.trim() || base.lines.join(' ')
  const preferredFont = resolveCaptionFontSize(seg, settings, frameW, frameH)
  const maxLines = 2
  let fontPx = preferredFont
  let lines = wrapCaptionText(text, caption.w * 0.9, fontPx, maxLines)
  while (
    fontPx > 10
    && lines.some((line) => measureLineWidth(line, fontPx) > caption.w * 0.98)
  ) {
    fontPx -= 1
    lines = wrapCaptionText(text, caption.w * 0.9, fontPx, maxLines)
  }
  const neededHeight = Math.ceil(fontPx * lines.length * 1.25 + 8)
  if (caption.h < neededHeight) {
    caption = fitBoxToCrop({
      ...caption,
      y: caption.y + (caption.h - neededHeight) / 2,
      h: neededHeight,
    }, crop)
  }
  const offsetY = Math.max(2, Math.round(fontPx * 0.06))
  const groupBottom = Math.max(
    fittedCover.y + fittedCover.h,
    caption.y + caption.h,
  )
  const shiftY = Math.max(0, Math.min(offsetY, crop.y + crop.h - groupBottom))
  const shiftedCover = { ...fittedCover, y: fittedCover.y + shiftY }
  caption = { ...caption, y: caption.y + shiftY }
  const union = unionBox(shiftedCover, caption)
  const padY = Math.max(2, Math.round(fontPx * 0.08))
  const coverY = Math.max(crop.y, caption.y - padY)
  const coverBottom = Math.min(crop.y + crop.h, caption.y + caption.h + padY)
  const cover = fitBoxToCrop({
    x: union.x,
    y: coverY,
    w: union.w,
    h: Math.max(4, coverBottom - coverY),
  }, crop)
  return {
    ...base,
    cover,
    caption,
    lines,
    fontPx,
    mask: sourceMask,
  }
}

export function estimatePreviewCaptionBox(
  ocr: PixelBox,
  text: string,
  fontSizePx: number,
  frameW: number,
  frameH: number,
  crop: CropRect,
  placement: 'over' | 'below' | 'above',
  lane?: PixelBox | null,
): PixelBox {
  if (cropCoversFull(crop, frameW, frameH)) {
    return estimateCaptionBox(ocr, text, fontSizePx, frameW, frameH, placement, lane)
  }
  const localOcr = {
    x: Math.max(0, ocr.x - crop.x),
    y: Math.max(0, ocr.y - crop.y),
    w: Math.min(ocr.w, crop.w),
    h: Math.min(ocr.h, crop.h),
  }
  const localLane = lane ? {
    x: Math.max(0, lane.x - crop.x),
    y: Math.max(0, lane.y - crop.y),
    w: Math.min(lane.w, crop.w),
    h: Math.min(lane.h, crop.h),
  } : null
  const box = estimateCaptionBox(localOcr, text, fontSizePx, crop.w, crop.h, placement, localLane)
  return { x: box.x + crop.x, y: box.y + crop.y, w: box.w, h: box.h }
}

export function segmentWithLayout(
  seg: Segment,
  layout: OverLayout,
  fontPx: number,
  settings?: ProjectSettings,
): Segment {
  const family = seg.fontFamily || settings?.subtitleFontFamily || 'system'
  const fs = layout.fontPx ?? fontPx
  return {
    ...seg,
    fontFamily: family,
    fontSize: seg.fontSize && seg.fontSize > 0 ? seg.fontSize : 0,
    bbox: seg.bbox ?? { x: layout.cover.x, y: layout.cover.y, w: layout.cover.w, h: layout.cover.h },
    coverBox: seg.coverBox ?? null,
    captionBox: seg.captionBox ?? { x: layout.caption.x, y: layout.caption.y, w: layout.caption.w, h: layout.caption.h },
    captionLayout: toCaptionLayout(layout.caption, layout.lines, fs),
  }
}

/**
 * below/above (không cover): bake đúng khung chữ preview (`estimateCaptionBox`).
 * Không dùng resolveOverLayout — hàm đó chỉ trả layout khi cover / OCR overlay.
 */
export function resolveBelowAboveLayout(
  seg: Segment,
  settings: ProjectSettings,
  frameW: number,
  frameH: number,
  crop: CropRect,
  placement: 'below' | 'above',
  lane?: PixelBox | null,
): OverLayout | null {
  if (!seg.translation.trim()) return null
  setMeasureFontFamily(captionFontCss(seg.fontFamily || settings.subtitleFontFamily || 'system'))
  const preferred = resolveCaptionFontSize(seg, settings, frameW, frameH)
  const ocr =
    (seg.bbox ? clampCoverBox(seg.bbox, frameW, frameH) : null)
    ?? seedCoverBox(seg, frameW, frameH, preferred)
    ?? fallbackCoverBox(frameW, frameH, preferred)
  const fitFrameW = cropCoversFull(crop, frameW, frameH) ? frameW : crop.w
  const { lines, fontPx } = fitOutsideCaption(ocr, seg.translation, preferred, fitFrameW)
  const caption = estimatePreviewCaptionBox(ocr, seg.translation, fontPx, frameW, frameH, crop, placement, lane)
  return { cover: ocr, caption, lines, fontPx }
}

/** Caption ngoài bbox: co font để giữ một dòng trước, rồi mới cho xuống tối đa hai dòng. */
export function fitOutsideCaption(
  _ocr: PixelBox,
  text: string,
  preferred: number,
  frameW: number,
) {
  // Bottom-lane captions share the project/segment font. Do not derive size
  // from each OCR box: varying source glyph heights made adjacent cues jump.
  const baseFont = Math.max(12, Math.round(preferred || AUTO_SUBTITLE_FONT))
  const maxInnerW = Math.max(24, Math.round(frameW * 0.92))
  let fontPx = baseFont
  // Portrait captions stay compact: one line when possible, otherwise two.
  let lines = wrapCaptionText(text, maxInnerW, fontPx, 2)
  // First choice: one horizontal line at the shared font. Otherwise wrap to
  // two lines; shrink only when either line still exceeds the video width.
  while (
    fontPx > 12
    && lines.some((line) => measureLineWidth(line, fontPx) > maxInnerW)
  ) {
    fontPx -= 1
    lines = wrapCaptionText(text, maxInnerW, fontPx, 2)
  }
  return { lines, fontPx }
}

/** ponytail: tính dải hardsub bao trùm toàn bộ các hàng phụ đề verified trong nửa khung hình tương ứng. */
export function hardsubLaneForSegment(
  segments: Segment[],
  frameW: number,
  frameH: number,
  seg?: Segment | null,
): PixelBox | null {
  if (frameW <= 0 || frameH <= 0) return null
  const verified = segments
    .filter((s) => s.bboxDetected === true && s.bbox && s.layout !== 'vertical' && s.layout !== 'label')
    .map((s) => clampCoverBox(s.bbox!, frameW, frameH))
  if (!verified.length) {
    return seg?.bbox ? clampCoverBox(seg.bbox, frameW, frameH) : null
  }
  const center = seg?.bbox ? seg.bbox.y + seg.bbox.h / 2 : frameH * 0.84
  const isLower = center >= frameH / 2
  const peers = verified.filter((b) => (b.y + b.h / 2 >= frameH / 2) === isLower)
  if (!peers.length) {
    return seg?.bbox ? clampCoverBox(seg.bbox, frameW, frameH) : null
  }
  const segBox = seg?.bbox ? clampCoverBox(seg.bbox, frameW, frameH) : null
  const allBoxes = segBox ? [...peers, segBox] : peers
  const top = Math.max(0, Math.min(...allBoxes.map((b) => b.y)))
  const bottom = Math.min(frameH, Math.max(...allBoxes.map((b) => b.y + b.h)))
  return { x: 0, y: top, w: frameW, h: Math.max(24, bottom - top) }
}

/** Bake đúng layout đang hiện ở preview vào segment — Xuất bản khóa WYSIWYG. */
export function buildExportSegments(
  segments: Segment[],
  settings: ProjectSettings,
  frameW: number,
  frameH: number,
): Segment[] {
  const defaultFamily = settings.subtitleFontFamily || 'system'
  const stampFont = (seg: Segment): Segment => ({
    ...seg,
    fontFamily: seg.fontFamily || defaultFamily,
    ...(speakerTextColor(seg, settings) ? { textColor: speakerTextColor(seg, settings) } : {}),
  })
  if (!settings.burnSubs || frameW <= 0) {
    return segments.map(stampFont)
  }
  const place = captionPlacement(settings)
  const crop = resolveCropRect(frameW, frameH, settings.previewAspectRatio ?? 'original', settings.previewCrop)
  return segments.map((seg) => {
    const styledSeg = stampFont(seg)
    if (!styledSeg.translation.trim()) return styledSeg
    const isVertLabel = styledSeg.layout === 'vertical' || styledSeg.layout === 'label'
    if (isVertLabel) {
      const layout = resolvePreviewOverLayout(styledSeg, settings, frameW, frameH, crop)
      if (layout) {
        const fontPx = layout.fontPx ?? resolveCaptionFontSize(styledSeg, settings, frameW, frameH)
        return segmentWithLayout(styledSeg, layout, fontPx, settings)
      }
      return styledSeg
    }
    // Caption kéo tay riêng (captionBox): xuất đúng tọa độ người dùng đã kéo
    if (styledSeg.captionBox) {
      const norm = clampCoverBox(styledSeg.captionBox, frameW, frameH)
      const layout = resolvePreviewOverLayout(styledSeg, settings, frameW, frameH, crop, norm)
      if (layout) {
        const fontPx = layout.fontPx ?? resolveCaptionFontSize(styledSeg, settings, frameW, frameH)
        return segmentWithLayout(styledSeg, layout, fontPx, settings)
      }
    }
    // Chèn dưới/trên: bake mid + horizontal (không dọc/nhãn) — khớp preview emerald box
    if (place === 'below' || place === 'above') {
      const lane = hardsubLaneForSegment(segments, frameW, frameH, styledSeg)
      const baked = resolveBelowAboveLayout(styledSeg, settings, frameW, frameH, crop, place, lane)
      if (baked) {
        return segmentWithLayout(
          styledSeg,
          baked,
          baked.fontPx ?? resolveCaptionFontSize(styledSeg, settings, frameW, frameH),
          settings,
        )
      }
    }
    // Che chữ cũ (place === 'over')
    const layout = resolvePreviewOverLayout(styledSeg, settings, frameW, frameH, crop)
    if (layout) {
      const fontPx = layout.fontPx ?? resolveCaptionFontSize(styledSeg, settings, frameW, frameH)
      return segmentWithLayout(styledSeg, layout, fontPx, settings)
    }
    return styledSeg
  })
}

/** Caption trong cover: 1 dòng (co font) → 2 dòng; căn giữa. */
export function layoutCaptionInCover(
  cover: PixelBox,
  text: string,
  fontSizePx: number,
  _frameW: number,
): Pick<OverLayout, 'caption' | 'lines'> & { fontPx?: number } {
  const trimmed = text.trim()
  const maxInnerW = Math.max(4, cover.w - CAP_PAD_X * 2)
  const sharedOneLineFits =
    measureLineWidth(trimmed, fontSizePx) <= maxInnerW
    && Math.ceil(fontSizePx * 1.12 + 4) <= cover.h
  const fit1 = fitCaptionLines(trimmed, maxInnerW, fontSizePx, {
    minFont: 1,
    preferOneLine: true,
    maxLines: 1,
  })
  const font1 = fit1.fontPx

  const fit2 = fitCaptionLines(trimmed, maxInnerW, fontSizePx, {
    minFont: 1,
    preferOneLine: false,
    maxLines: 2,
  })
  let font2 = fit2.fontPx
  let lines2 = fit2.lines
  while (font2 > 8 && lines2.length > 1 && Math.ceil(lines2.length * font2 * 1.12 + 4) > cover.h) {
    font2 -= 1
    const refit = fitCaptionLines(trimmed, maxInnerW, font2, { preferOneLine: false, maxLines: 2, minFont: 1 })
    font2 = refit.fontPx
    lines2 = refit.lines
  }
  if (Math.ceil(lines2.length * font2 * 1.12 + 4) > cover.h) {
    font2 = 0
  }

  let fontPx: number
  let lines: string[]
  if (sharedOneLineFits) {
    fontPx = fontSizePx
    lines = [trimmed]
  } else if (font2 > font1 && lines2.length > 1) {
    fontPx = font2
    lines = lines2
  } else {
    const forcedFit = fitCaptionLines(trimmed, maxInnerW, fontSizePx, { minFont: 1, preferOneLine: false, maxLines: 1 })
    fontPx = forcedFit.fontPx
    lines = forcedFit.lines
  }

  while (fontPx > 8 && lines.some((line) => measureLineWidth(line, fontPx) > maxInnerW)) {
    fontPx -= 1
    lines = wrapCaptionText(trimmed, maxInnerW, fontPx, Math.max(1, lines.length))
  }

  const lineH = fontPx * 1.12
  const textBlockH = Math.ceil(lines.length * lineH + 4)
  const textW = Math.max(...lines.map((l) => measureLineWidth(l, fontPx)), 1)
  const inkCapW = Math.max(...lines.map((l) => lineNeedWidth(l, fontPx)), Math.ceil(textW + CAP_PAD_X * 2))
  // 1 dòng: caption = gần full cover (khung vàng không lệch hẹp một bên)
  const edge = Math.max(2, Math.round(cover.w * 0.01))
  const captionW = Math.min(
    cover.w,
    Math.ceil(lines.length === 1 ? Math.max(inkCapW, cover.w - edge * 2) : inkCapW),
  )
  const cx = cover.x + cover.w / 2
  const captionX = Math.round(Math.max(cover.x, Math.min(cover.x + cover.w - captionW, cx - captionW / 2)))
  const captionY = captionCenterInCover(cover.y, cover.h, textBlockH)
  return {
    caption: { x: captionX, y: captionY, w: captionW, h: textBlockH },
    lines,
    fontPx: fontPx !== fontSizePx ? fontPx : undefined,
  }
}

/** Tự co/giãn cover: full ngang được; 1 dòng (co font) rồi 2 dòng. */
export function adaptiveCoverLayout(
  cover: PixelBox,
  text: string,
  fontSizePx: number,
  frameW: number,
  frameH: number,
): OverLayout {
  const pad = coverPad(fontSizePx, frameW)
  const cx = cover.x + cover.w / 2
  const topY = cover.y
  const trimmed = text.trim()
  const maxInnerW = frameMaxInnerWidth(fontSizePx, frameW)
  let { lines, fontPx } = fitCaptionLines(trimmed, maxInnerW, fontSizePx, {
    preferOneLine: true,
    maxLines: 2,
  })

  const sizeFromLines = (ls: string[], fs: number) => {
    const lineH = fs * 1.12
    const textBlockH = Math.ceil(ls.length * lineH + 4)
    const textW = Math.max(...ls.map((l) => measureLineWidth(l, fs)), 1)
    const inkCapW = Math.max(...ls.map((l) => lineNeedWidth(l, fs)), Math.ceil(textW + CAP_PAD_X * 2))
    const coverW = Math.min(frameW, Math.max(cover.w, inkCapW + pad.x * 2))
    const edge = Math.max(2, Math.round(coverW * 0.01))
    const captionW = Math.ceil(ls.length === 1 ? Math.max(inkCapW, coverW - edge * 2) : inkCapW)
    const byText = textBlockH + pad.top + pad.bottom + COVER_SHADOW_BOT
    const coverH = Math.min(frameH, Math.max(cover.h, byText))
    return { lineH, textBlockH, textW, captionW, coverW, coverH }
  }

  let { textBlockH, captionW, coverW, coverH } = sizeFromLines(lines, fontPx)
  let coverX = Math.round(Math.max(0, Math.min(frameW - coverW, cx - coverW / 2)))
  let coverY = Math.round(Math.max(0, Math.min(frameH - coverH, topY)))
  let box = clampCoverBox({ x: coverX, y: coverY, w: coverW, h: coverH }, frameW, frameH)

  const inner = coverInnerWidth(box.w, fontPx, frameW)
  const refit = fitCaptionLines(trimmed, inner, fontPx, { preferOneLine: true, maxLines: 2 })
  if (refit.lines.join('\n') !== lines.join('\n') || refit.fontPx !== fontPx) {
    lines = refit.lines
    fontPx = refit.fontPx
    const sized = sizeFromLines(lines, fontPx)
    textBlockH = sized.textBlockH
    captionW = sized.captionW
    coverW = sized.coverW
    coverH = sized.coverH
    coverX = Math.round(Math.max(0, Math.min(frameW - coverW, cx - coverW / 2)))
    coverY = Math.round(Math.max(0, Math.min(frameH - coverH, topY)))
    box = clampCoverBox({ x: coverX, y: coverY, w: coverW, h: coverH }, frameW, frameH)
  }

  const capX = Math.round(Math.max(box.x, Math.min(box.x + box.w - captionW, box.x + box.w / 2 - captionW / 2)))
  const capY = captionCenterInCover(box.y, box.h, textBlockH)
  return {
    cover: box,
    caption: { x: capX, y: capY, w: Math.min(captionW, box.w), h: textBlockH },
    lines,
    fontPx,
  }
}

export function manualCoverLayout(
  cover: PixelBox,
  text: string,
  fontSizePx: number,
  frameW: number,
  frameH: number,
  fixed = false,
  allowExpand = true,
): OverLayout {
  if (fixed) {
    let box = clampCoverBox(cover, frameW, frameH)
    // Automatic horizontal captions keep the shared font: expand to one line
    // first, otherwise to a two-line block. Shrink happens only after the
    // shared block has exhausted the frame.
    const sharedLines = wrapCaptionText(
      text.trim(),
      frameMaxInnerWidth(fontSizePx, frameW),
      fontSizePx,
      2,
    )
    const padX = coverPad(fontSizePx, frameW).x
    const sharedNeedW = Math.ceil(
      Math.max(...sharedLines.map((line) => lineNeedWidth(line, fontSizePx)), 1) + padX * 2,
    )
    const sharedNeedH = Math.ceil(sharedLines.length * fontSizePx * 1.12 + 4)
    if (
      allowExpand
      && text.trim()
      && sharedLines.every((line) => measureLineWidth(line, fontSizePx) <= frameMaxInnerWidth(fontSizePx, frameW))
      && (sharedNeedW > box.w || sharedNeedH > box.h)
    ) {
      const cy = box.y + box.h / 2
      const h = Math.min(frameH, Math.max(box.h, sharedNeedH))
      box = expandCoverCentered(box, sharedNeedW, frameW, frameH)
      box = clampCoverBox(
        { ...box, y: Math.round(Math.max(0, Math.min(frameH - h, cy - h / 2))), h },
        frameW,
        frameH,
      )
    }
    let laid = layoutCaptionInCover(box, text, fontSizePx, frameW)
    const fs = laid.fontPx ?? fontSizePx
    const inkNeedW = Math.ceil(
      Math.max(...laid.lines.map((line) => lineNeedWidth(line, fs)), 1) + padX * 2,
    )
    if (allowExpand && inkNeedW > box.w) {
      box = expandCoverCentered(box, inkNeedW, frameW, frameH)
      laid = layoutCaptionInCover(box, text, fontSizePx, frameW)
    }

    return {
      cover: box,
      caption: laid.caption,
      lines: laid.lines,
      fontPx: laid.fontPx ?? fontSizePx,
    }
  }
  return adaptiveCoverLayout(cover, text, fontSizePx, frameW, frameH)
}

/** Fit chu trong bbox co dinh — keo/tha caption ngang (khong lien quan mid). */
export function fitFixedCoverCaption(
  cover: PixelBox,
  text: string,
  frameW: number,
  frameH: number,
): OverLayout {
  const startFs = autoFontFromBbox(cover, text, 0)
  return manualCoverLayout(cover, text, startFs, frameW, frameH, true)
}


/** Khung chữ dịch — below/above hoặc fallback */
export function tightCaptionTextBox(
  text: string,
  fontSizePx: number,
  frameW: number,
  frameH: number,
  wrapW?: number,
  maxLines = 3,
): PixelBox {
  const pad = coverPad(fontSizePx, frameW)
  const innerW = wrapW ?? Math.min(frameW, Math.round(frameW * 0.88))
  const lines = wrapCaptionText(text, innerW, fontSizePx, maxLines)
  const lineH = fontSizePx * 1.12
  const textW = Math.min(innerW, Math.max(...lines.map((l) => measureLineWidth(l, fontSizePx)), 1))
  return {
    x: 0,
    y: 0,
    w: Math.min(frameW, Math.ceil(textW + pad.x * 2)),
    h: Math.min(frameH, Math.ceil(lines.length * lineH + pad.top + pad.bottom)),
  }
}

export function fitCoverBoxOver(
  anchor: PixelBox,
  text: string,
  fontSizePx: number,
  frameW: number,
  frameH: number,
  sourceText = '',
): PixelBox {
  return layoutOverMode(anchor, text, fontSizePx, frameW, frameH, sourceText).cover
}

/** Font preview: cqw theo chiều ngang caption (1 dòng), cqh khi nhiều dòng */
export function captionFontStyle(
  fontPx: number,
  boxSource: number,
  axis: 'w' | 'h' = 'h',
): React.CSSProperties {
  if (boxSource <= 0) return { fontSize: fontPx }
  const unit = axis === 'w' ? 'cqw' : 'cqh'
  return { fontSize: `calc(100${unit} * ${fontPx / boxSource})` }
}

/**
 * Overlay mid/dọc/nhãn: scale theo fontPx nguồn / kích thước cover
 * (không dùng cqh/n — công thức cũ bỏ qua fontPx nên kéo cỡ không ăn).
 */
export function overlayDisplayFontStyle(
  layout: 'vertical' | 'label' | 'mid',
  cover: PixelBox,
  fontPx: number,
  _lineCount: number,
): React.CSSProperties {
  const w = Math.max(1, cover.w)
  const h = Math.max(1, cover.h)
  const byW = Math.min(0.98, fontPx / w)
  const byH = Math.min(0.98, fontPx / h)
  if (layout === 'vertical') {
    // kẹp theo fontPx/cột — không fill full cqh (chữ to hơn bbox)
    return {
      fontSize: `min(calc(100cqw * ${byW}), calc(100cqh * ${byH}))`,
      lineHeight: 1.05,
      maxWidth: '100%',
      width: '100%',
      height: '100%',
      overflow: 'hidden',
    }
  }
  // mid/label: scale ≤ fontPx/box — không phình cqh (chữ to tràn bbox)
  if (layout === 'mid' || layout === 'label') {
    const n = Math.max(1, _lineCount)
    const lh = layout === 'mid' ? 1.1 : 1.12
    const fracH = Math.min(fontPx / h, 0.95 / (n * lh))
    const fracW = Math.min(0.98, fontPx / w)
    return {
      fontSize: `min(calc(100cqw * ${fracW}), calc(100cqh * ${fracH}))`,
      lineHeight: lh,
      maxWidth: '100%',
      width: '100%',
      height: '100%',
      overflow: 'hidden',
      padding: layout === 'mid' ? `0 ${OCR_MID_PAD_EM}em` : '0 6px',
      boxSizing: 'border-box' as const,
    }
  }
  return {
    fontSize: `min(calc(100cqw * ${byW}), calc(100cqh * ${byH}))`,
    lineHeight: 1.1,
    maxWidth: '100%',
  }
}

/** ponytail: self-check — below/above bake đúng lane preview (đáy cho auto, bbox cho kéo tay). */
export function __checkExportBakePlacement(): void {
  const settings = {
    burnSubs: true,
    coverHardsubs: false,
    captionPlacement: 'below',
    subtitleFontSize: 0,
    subtitleFontFamily: 'system',
    targetLang: 'vi',
    previewAspectRatio: 'original',
  } as unknown as ProjectSettings
  const seg = {
    id: 's1', index: 0, start: 0, end: 2, source: '你好', translation: 'Xin chào',
    voice: '', layout: 'mid', bbox: { x: 300, y: 800, w: 400, h: 80 },
  } as unknown as Segment
  const [baked] = buildExportSegments([seg], settings, 1080, 1920)
  const cl = baked.captionLayout
  if (!cl) throw new Error('below bake must produce captionLayout')
  if (cl.y < 880) throw new Error('auto mid must bake the below lane, got y=' + cl.y)
  const aboveSettings = { ...settings, captionPlacement: 'above' } as unknown as ProjectSettings
  const [bakedAbove] = buildExportSegments([seg], aboveSettings, 1080, 1920)
  const clAbove = bakedAbove.captionLayout
  if (!clAbove) throw new Error('above bake must produce captionLayout')
  if (clAbove.y + clAbove.h > 800) throw new Error('above mid must stay above hardsub box, got bottom=' + (clAbove.y + clAbove.h))
  const dragged = { ...seg, id: 's2', bboxInherited: false } as Segment
  const [bakedDrag] = buildExportSegments([dragged], settings, 1080, 1920)
  const cl2 = bakedDrag.captionLayout
  if (!cl2) throw new Error('dragged bake must produce captionLayout')
  if (cl2.y >= 880) throw new Error('dragged mid must stay anchored in its bbox, got y=' + cl2.y)
}

/** Ước lượng vị trí phụ đề — below/above: cỡ ≈ bbox che, neo sát trên/dưới dải OCR hoặc dải hardsub. */
export function estimateCaptionBox(
  ocr: PixelBox,
  text: string,
  fontSizePx: number,
  frameW: number,
  frameH: number,
  placement: 'over' | 'below' | 'above',
  lane?: PixelBox | null,
): PixelBox {
  if (placement === 'over') return layoutOverMode(ocr, text, fontSizePx, frameW, frameH, '').caption

  // Caption đáy/trên ưu tiên một dòng trên bề rộng video; chỉ xuống dòng
  // khi đã co tới giới hạn đọc được. Dùng chung với preview/export bake.
  const fitted = fitOutsideCaption(ocr, text, fontSizePx, frameW)
  const fs = fitted.fontPx
  const wrapW = Math.max(24, Math.round(frameW * 0.92))
  const textBox = tightCaptionTextBox(text, fs, frameW, frameH, wrapW, Math.max(1, fitted.lines.length))
  const gap = Math.max(3, Math.round(fs * 0.18))
  // below/above: căn giữa theo bề rộng video, không theo tâm bbox OCR
  // (bbox OCR có thể ngắn/lệch → caption bị lệch so với chữ gốc)
  const cx = frameW / 2
  // ponytail: neo vào dải hardsub để phụ đề dịch không đè lên câu thoại 2 dòng/dải hardsub
  const topAnchor = lane ? Math.min(ocr.y, lane.y) : ocr.y
  const bottomAnchor = lane ? Math.max(ocr.y + ocr.h, lane.y + lane.h) : ocr.y + ocr.h
  const belowY = bottomAnchor + gap
  const aboveY = topAnchor - gap - textBox.h
  let y0: number
  if (placement === 'below') {
    // Near an edge, clamping the requested lane can put its text straight on
    // top of the source subtitle.  Flip only when that is the sole free side.
    y0 = belowY <= frameH - textBox.h ? belowY : Math.max(0, aboveY)
  } else {
    y0 = aboveY >= 0 ? aboveY : Math.min(frameH - textBox.h, belowY)
  }
  const x0 = Math.max(0, Math.min(frameW - textBox.w, Math.round(cx - textBox.w / 2)))
  return { x: x0, y: y0, w: textBox.w, h: textBox.h }
}
