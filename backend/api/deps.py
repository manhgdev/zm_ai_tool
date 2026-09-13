"""Shared API helpers / schemas used by routes."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from pipeline.core.media import video_size


class Settings(BaseModel):
    workflow: str | None = None
    engine: str = "whisper"
    subtitleSource: str = ""
    sourceLang: str = "auto"
    targetLang: str = "vi"
    translator: str = "google"
    ollamaMode: str = "cloud"
    ollamaModel: str = "minimax-m3:cloud"
    ollamaLocalTier: str = "balanced"
    matchDuration: str = "preferVideo"
    defaultVoice: str = "cc:BV075_streaming:7102355803792740865"
    stableCaptionLocate: bool = False
    analysisRegion: dict[str, float] | None = None
    # Giữ nguyên cờ từ UI. Nếu thiếu field này Pydantic sẽ bỏ nó khỏi
    # request, khiến exporter không bao giờ chạy bước che logo.
    coverLogo: bool = False
    # Watermarks the user explicitly chose to leave visible.  Kept separate
    # from coverLogo so each detected logo can be toggled independently.
    hiddenLogoTexts: list[str] = []
    coverHardsubs: bool = True
    blurBandMode: Literal["off", "auto", "manual"] = "auto"
    blurBandRegion: dict[str, float] | None = None
    blurBandAutoRegion: dict[str, float] | None = None
    blurBandAutoRegionVersion: int = 0
    coverMaskStyle: str = "blur"
    coverMaskColor: str = "#4c1d95"
    coverMaskOpacity: int = 0
    burnSubs: bool = True
    captionPlacement: str = "above"
    subtitleFontSize: int = 0
    subtitleFontFamily: str = "system"
    captionTextColor: str = "#ffffff"
    captionBgStyle: str = "none"
    captionBgColor: str = "#000000"
    captionBgOpacity: int = 55
    captionStroke: bool = True
    sourceSubtitleVisible: bool = False
    dubSubtitleVisible: bool = True
    subtitleExportTrack: str = "dub"
    colorAdjust: dict[str, float] = Field(default_factory=dict)
    lutAssetId: str = ""
    processOriginalAudio: bool = False
    originalAudioMode: str = "original"
    originalAudioVolume: int = 100
    # Ô Preview trên UI (s) — không phải cửa sổ lần dịch
    previewSec: int = 20
    # Cửa sổ lần chạy: 0=full, N=preview Ns; None=legacy (dùng previewSec)
    runPreviewSec: int | None = None
    workers: int = 0
    previewAspectRatio: str = "original"
    previewCrop: dict[str, float] | None = None
    videoScaleX: float = 100.0
    videoScaleY: float = 100.0
    videoScale: float | None = None
    exportResolution: str = "1080"
    exportVideo: bool = True
    exportVideoFormat: str = "mp4"
    exportAudio: bool = False
    exportAudioFormat: str = "mp3"
    exportSrt: bool = False
    exportSrtFormat: str = "srt"
    # Thư mục xuất tùy chọn (FE gửi kèm body export) — thiếu field này
    # Pydantic strip mất, video luôn rơi vào thư mục mặc định.
    exportOutputDir: str = ""
    exportGif: bool = False
    exportGifRes: str = "240"
    forceTts: bool = False
    speakerDiarization: bool = False
    speakerCount: int = 0
    speakerVoices: dict[str, str] = Field(default_factory=dict)
    speakerProfiles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    speakerCaptionColors: bool = False


class SegmentIn(BaseModel):
    # CapCut cloud projects created before the timeline migration did not
    # persist these two editor fields.  Accept them here; the export route
    # assigns a stable id before handing clips to the renderer.
    id: str = ""
    index: int = 0
    start: float
    end: float
    source: str
    translation: str
    sourceSubtitle: str | None = None
    dubSubtitle: str | None = None
    voice: str
    speaker: str | None = None
    audioUrl: str | None = None
    audioFile: str | None = None
    audioDuration: float | None = None
    coverStart: float | None = None
    coverEnd: float | None = None
    layout: str | None = None
    dub: bool | None = None
    bbox: dict[str, float] | None = None
    bboxInherited: bool | None = None
    bboxDetected: bool | None = None
    videoSpeed: float | None = None
    ttsVolume: float | None = None
    ttsSpeed: float | None = None
    # Bake speed tại thời điểm fit TTS — playback = ttsSpeed × (bake hiện tại / ttsBake)
    ttsBake: float | None = None
    fontSize: int | None = None
    fontFamily: str | None = Field(
        default=None,
        pattern=r"^(?:system|segoe|arial|bold|helvetica|verdana|tahoma|trebuchet|rounded|impact|georgia|times|palatino|garamond|courier|mono|comic|cjk|meiryo|malgun)$",
    )
    textColor: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    coverBox: dict[str, Any] | None = None
    captionBox: dict[str, Any] | None = None
    captionLayout: dict[str, Any] | None = None
    groupId: str | None = None
    isCompound: bool | None = None
    compoundChildren: list[dict[str, Any]] | None = None


class ExportPayload(Settings):
    segments: list[SegmentIn] | None = None
    exportEndSec: float | None = None
    exportStartSec: float | None = None
    renderName: str = Field(min_length=1, max_length=120)
    coverDataUrl: str | None = None


class TextOverlayIn(BaseModel):
    id: str
    start: float
    end: float
    text: str = ""
    x: float
    y: float
    w: float
    h: float
    fontSize: int = 42
    fontFamily: str | None = None
    color: str = "#ffffff"
    kind: str | None = "text"
    maskStyle: str | None = None
    maskColor: str | None = None
    maskOpacity: int | None = None
    # Marks an automatically detected static watermark. It must round-trip
    # through the API so the editor does not repeatedly migrate/persist it.
    watermarkSource: str | None = None
    ocrSource: str | None = None
    track: str | None = None
    logoSource: str | None = None
    assetUrl: str | None = None
    iconId: str | None = None
    scope: str | None = None
    motion: str | None = None
    opacity: int | None = None
    zIndex: int | None = None
    blendMode: str | None = None
    keyframes: list[dict[str, float]] | None = None
    visibleSec: float | None = None
    hiddenSec: float | None = None
    fadeSec: float | None = None
    safeMargin: float | None = None
    positionSeed: int | None = None
    positionKeyframes: list[dict[str, float]] | None = None


class CloudBlock(BaseModel):
    apiKey: str | None = None
    apiKeys: str | None = None
    keys: list[str] | None = None
    baseUrl: str | None = None
    model: str | None = None


class ElevenLabsBlock(BaseModel):
    apiKeys: str | None = None
    keys: list[str] | None = None


class TtsBlock(BaseModel):
    elevenlabs: ElevenLabsBlock | None = None


class AppConfigIn(BaseModel):
    cloud: dict[str, CloudBlock] | None = None
    tts: TtsBlock | None = None


class UiPreferencesIn(BaseModel):
    locale: Literal["vi", "en"] | None = None
    storage: dict[str, str] | None = None
    outputRoot: str | None = None  # absolute path; empty string = reset to default


class PreviewTtsIn(BaseModel):
    text: str
    voice: str = "el:pNInz6obpgDQGcFmaJgB"
    lang: str = "vi"
    speed: float = 1.0


class RebakeSpeedIn(BaseModel):
    speed: float = 1.0
    skipRemap: bool = False
    # FE speedRevision — request cũ (rev nhỏ hơn) bị bỏ, không ghi đè
    speedRevision: int | None = None


class RetranslateIn(BaseModel):
    text: str = ""
    sourceLang: str | None = None
    targetLang: str | None = None
    translator: str | None = None
    ollamaMode: str | None = None
    ollamaModel: str | None = None
    ollamaLocalTier: str | None = None


class StudioSynthIn(BaseModel):
    jobId: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    text: str = ""
    srtText: str = ""
    voice: str = "system"
    speaker_id: str | None = None
    lang: str = "vi"
    speed: float = 1.0
    volume: float = 1.0
    pitch: float = 0.0
    style: str = "tu_nhien"
    matchDuration: str = "none"
    keepTimeline: bool = False
    autoSplit: bool = False
    gapMs: int = 0
    title: str = ""
    outputDir: str = ""
    outputFormat: str = "wav48"
    publishOutput: bool = False


def validate_segment_editor_fields(body: SegmentIn, meta: dict) -> None:
    if body.videoSpeed is not None:
        if not math.isfinite(body.videoSpeed) or not 0.5 <= body.videoSpeed <= 2.0:
            raise HTTPException(422, "videoSpeed phải nằm trong khoảng 0.5–2.0")
    if body.ttsVolume is not None and (
        not math.isfinite(body.ttsVolume) or not 0 <= body.ttsVolume <= 200
    ):
        raise HTTPException(422, "ttsVolume phải nằm trong khoảng 0–200")
    if body.ttsSpeed is not None and (
        not math.isfinite(body.ttsSpeed) or not 0.75 <= body.ttsSpeed <= 1.5
    ):
        raise HTTPException(422, "ttsSpeed phải nằm trong khoảng 0.75–1.5")
    if body.fontSize is not None and body.fontSize != 0 and not 12 <= body.fontSize <= 240:
        raise HTTPException(422, "fontSize phải là 0 (tự động) hoặc 12–240 px")
    if body.bbox is None:
        return
    keys = {"x", "y", "w", "h"}
    if set(body.bbox) != keys:
        raise HTTPException(422, "bbox cần đủ x, y, w, h")
    x, y, bw, bh = (float(body.bbox[key]) for key in ("x", "y", "w", "h"))
    if not all(math.isfinite(value) for value in (x, y, bw, bh)) or bw <= 0 or bh <= 0:
        raise HTTPException(422, "bbox không hợp lệ")
    width, height = video_size(Path(meta["videoPath"]))
    if x < 0 or y < 0 or x + bw > width or y + bh > height:
        raise HTTPException(422, "bbox nằm ngoài khung video")
    body.bbox = {"x": x, "y": y, "w": bw, "h": bh}


def require_meta(project_id: str) -> dict:
    from pipeline import load_meta

    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404, "Project not found")
    return meta


class CompoundClipIn(BaseModel):
    segmentIds: list[str] = Field(default_factory=list)


class CloneRenameIn(BaseModel):
    name: str = ""


class VoicePatchIn(BaseModel):
    name: str | None = None
    tags: list[str] | None = None
    language: str | None = None
    favorite: bool | None = None
    # zmai | clone — chuyển bucket (chỉ local VieNeu ref)
    engine: str | None = None


class VoiceBulkMoveIn(BaseModel):
    voiceIds: list[str] = Field(default_factory=list)
    target: str = ""


# Field editor có thể bỏ sót khi PUT full list — giữ từ meta cũ theo id
SEG_PRESERVE = (
    "speaker",
    "audioUrl",
    "audioFile",
    "audioDuration",
    "bbox",
    "bboxInherited",
    "bboxDetected",
    "coverBox",
    "captionBox",
    "captionLayout",
    "layout",
    "dub",
    "videoSpeed",
    "ttsVolume",
    "ttsSpeed",
    "ttsBake",
    "fontSize",
    "fontFamily",
    "textColor",
    "coverStart",
    "coverEnd",
    "groupId",
    "isCompound",
    "compoundChildren",
)


def validate_overlay(body: TextOverlayIn, meta: dict) -> None:
    import math
    from pathlib import Path
    from fastapi import HTTPException
    from pipeline.core.media import video_size

    width, height = video_size(Path(meta["videoPath"]))
    values = (body.start, body.end, body.x, body.y, body.w, body.h)
    if not all(math.isfinite(value) for value in values) or body.end <= body.start:
        raise HTTPException(422, "Thời gian text không hợp lệ")
    if (
        body.x < 0
        or body.y < 0
        or body.w <= 0
        or body.h <= 0
        or body.x + body.w > width
        or body.y + body.h > height
    ):
        raise HTTPException(422, "Text nằm ngoài khung video")
