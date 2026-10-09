"""Optional offline speaker diarization using Sherpa-ONNX."""
from __future__ import annotations

import math
import os
import shutil
import uuid
from pathlib import Path
from typing import Any


SEGMENTATION_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMBEDDING_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
EMBEDDING_NAME = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"

# First-time role/voice mapping after diarization. Speaker IDs are assigned by
# Sherpa in stable numeric order (SPEAKER_00, SPEAKER_01, …), not by gender.
# These are editable defaults, never a claim about a detected person's gender.
DEFAULT_SPEAKER_ROLES = (
    "Nam chính", "Nữ chính", "Nữ phụ", "Nam phụ", "Người dẫn truyện", "Khách mời 1", "Khách mời 2",
)
DEFAULT_SPEAKER_VOICES = (
    "cc:BV075_streaming:7102355803792740865",  # Thanh Niên Tự Tin
    "cc:BV074_streaming:7102355709945188865",  # Cô Gái Hoạt Ngôn
    "cc:BV421_vivn_streaming:7252594014782755330",  # Nhỏ Ngọt Ngào
    "cc:BV560_streaming:7483736167565758992",  # Alex Đại Đế
    "cc:multi_female_richgirl_uranus_bigtts:7637460351541447956",  # Review Phim new
    "cc:BV560_streaming:7483736167565758992",  # Alex Đại Đế
    "cc:BV562_streaming:7483736254694035984",  # Mai
)


def default_speaker_role(position: int) -> str:
    return DEFAULT_SPEAKER_ROLES[position] if position < len(DEFAULT_SPEAKER_ROLES) else f"Người nói {position + 1}"


def default_speaker_voice(position: int, fallback: str) -> str:
    return DEFAULT_SPEAKER_VOICES[position] if position < len(DEFAULT_SPEAKER_VOICES) else fallback


def is_generated_speaker_role(name: object) -> bool:
    value = str(name or "").strip()
    return not value or value in DEFAULT_SPEAKER_ROLES or value.startswith("Người nói ") or value.startswith("Speaker ")



_DOWNLOAD_TIMEOUT = 600  # 10 phút tối đa mỗi file model (~100MB qua mạng chậm)
_DOWNLOAD_RETRIES = 2


def _download_file(url: str, dest: Path, log=None, progress=None) -> None:
    """Download url → dest (atomic), với timeout và retry."""
    import time

    def report(msg: str) -> None:
        if log:
            log(msg + "\n")

    partial = dest.with_suffix(dest.suffix + f".part-{os.getpid()}")
    partial.unlink(missing_ok=True)
    last_err: Exception | None = None
    for attempt in range(1, _DOWNLOAD_RETRIES + 2):
        try:
            report(f"{'Thử lại — ' if attempt > 1 else ''}Đang tải: {url.split('/')[-1]}")
            from urllib.parse import unquote, urlparse

            parsed = urlparse(url)
            if parsed.scheme == "file":
                # Local files are used by offline tests and must not go through
                # httpx/proxy parsing at all.
                shutil.copyfile(Path(unquote(parsed.path)), partial)
            else:
                # macOS environment variables commonly include bare IPv6
                # values (::1) that httpx mistakes for an invalid proxy port.
                from pipeline.core.config import sanitize_httpx_no_proxy
                sanitize_httpx_no_proxy()
                try:
                    import httpx
                    with httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                        with client.stream("GET", url) as resp:
                            resp.raise_for_status()
                            total = int(resp.headers.get("content-length", 0))
                            downloaded = 0
                            with partial.open("wb") as fh:
                                for chunk in resp.iter_bytes(chunk_size=1 << 20):  # 1 MB
                                    fh.write(chunk)
                                    downloaded += len(chunk)
                                    if progress:
                                        progress(downloaded, total)
                                    if total:
                                        pct = int(downloaded * 100 / total)
                                        if pct % 10 == 0:
                                            report(f"  {pct}% ({downloaded // (1 << 20)} / {total // (1 << 20)} MB)")
                except ImportError:
                    # fallback: urllib (no progress, but works everywhere)
                    import socket
                    import urllib.request as _ur

                    report("  (dùng urllib fallback)")
                    old_timeout = socket.getdefaulttimeout()
                    socket.setdefaulttimeout(_DOWNLOAD_TIMEOUT)
                    try:
                        def reporthook(blocks: int, block_size: int, total: int) -> None:
                            if progress:
                                progress(blocks * block_size, max(0, total))

                        _ur.urlretrieve(url, partial, reporthook=reporthook)
                    finally:
                        socket.setdefaulttimeout(old_timeout)

            # Validate size
            size = partial.stat().st_size
            if parsed.scheme != "file" and size < 1024:
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"File tải về quá nhỏ ({size} bytes) — có thể lỗi mạng")

            # Atomic replace — Windows safe
            try:
                partial.replace(dest)
            except OSError:
                # WinError 32: file bị lock → copy thay vì rename
                import shutil as _shutil
                _shutil.copy2(partial, dest)
                partial.unlink(missing_ok=True)

            report(f"  ✓ Xong — {dest.stat().st_size // (1 << 20)} MB")
            return

        except Exception as e:
            last_err = e
            partial.unlink(missing_ok=True)
            if attempt <= _DOWNLOAD_RETRIES:
                wait = 3 * attempt
                report(f"  Lỗi: {e} — thử lại sau {wait}s…")
                time.sleep(wait)

    raise RuntimeError(
        f"Không tải được {url.split('/')[-1]} sau {_DOWNLOAD_RETRIES + 1} lần: {last_err}"
    )


def ensure_diarization_models(model_dir: Path, log=None, progress=None) -> tuple[Path, Path]:
    """Download the two official Sherpa models once, with atomic destination writes."""
    model_dir.mkdir(parents=True, exist_ok=True)
    segmentation = model_dir / "model.int8.onnx"
    embedding = model_dir / EMBEDDING_NAME
    if segmentation.is_file() and embedding.is_file():
        return segmentation, embedding

    def report(message: str) -> None:
        if log:
            log(message + "\n")

    if not segmentation.is_file():
        report("Đang tải model phân đoạn người nói (segmentation)…")
        try:
            # Tải trực tiếp nếu URL là file .onnx đơn giản
            # Nếu URL là tar.bz2 thì cần extract
            import tempfile
            with tempfile.TemporaryDirectory(prefix="zm_ai_tool-diarization-") as tmp_raw:
                tmp = Path(tmp_raw)
                archive = tmp / "segmentation.tar.bz2"
                _download_file(
                    SEGMENTATION_URL,
                    archive,
                    log,
                    (lambda current, total: progress("segmentation", current, total))
                    if progress else None,
                )
                report("Đang giải nén model phân đoạn…")
                import tarfile
                with tarfile.open(archive, "r:bz2") as bundle:
                    member = next(
                        (m for m in bundle.getmembers()
                         if Path(m.name).name == "model.int8.onnx" and m.isfile()),
                        None,
                    )
                    if member is None:
                        raise RuntimeError("Gói model diarization không chứa model.int8.onnx")
                    source = bundle.extractfile(member)
                    if source is None:
                        raise RuntimeError("Không đọc được model phân đoạn người nói")
                    partial = model_dir / "model.int8.onnx.part"
                    with source, partial.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    try:
                        partial.replace(segmentation)
                    except OSError:
                        shutil.copy2(partial, segmentation)
                        partial.unlink(missing_ok=True)
        except Exception as e:
            segmentation.unlink(missing_ok=True)
            raise RuntimeError(f"Không tải được model phân đoạn: {e}") from e

    if not embedding.is_file():
        report("Đang tải model nhận dạng giọng người nói (embedding)…")
        try:
            _download_file(
                EMBEDDING_URL,
                embedding,
                log,
                (lambda current, total: progress("embedding", current, total))
                if progress else None,
            )
        except Exception as e:
            embedding.unlink(missing_ok=True)
            raise RuntimeError(f"Không tải được model embedding: {e}") from e

    report("✓ Đã cài model tách người nói thành công.")
    return segmentation, embedding



_MIN_TURN_SECONDS = 0.15
_AUTO_CLUSTER_THRESHOLD = 0.94


def normalize_speaker_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort, de-noise, and join tiny gaps in the diarizer output."""
    cleaned: list[dict[str, Any]] = []
    for turn in turns or []:
        try:
            start = float(turn["start"])
            end = float(turn["end"])
            speaker = str(turn["speaker"] or "").strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not speaker or not math.isfinite(start) or not math.isfinite(end):
            continue
        if end - start < _MIN_TURN_SECONDS:
            continue
        cleaned.append({"start": start, "end": end, "speaker": speaker})

    merged: list[dict[str, Any]] = []
    for turn in sorted(cleaned, key=lambda item: (item["start"], item["end"], item["speaker"])):
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous["speaker"] == turn["speaker"]
            and turn["start"] <= previous["end"] + 0.12
        ):
            previous["end"] = max(previous["end"], turn["end"])
        else:
            merged.append(turn)
    return merged


def _coverage_by_speaker(start: float, end: float, turns: list[dict[str, Any]]) -> dict[str, float]:
    spans: dict[str, list[tuple[float, float]]] = {}
    for turn in turns:
        speaker = str(turn.get("speaker") or "").strip()
        if not speaker:
            continue
        try:
            left = max(start, float(turn["start"]))
            right = min(end, float(turn["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if right > left:
            spans.setdefault(speaker, []).append((left, right))

    coverage: dict[str, float] = {}
    for speaker, intervals in spans.items():
        total = 0.0
        previous_end = start
        for left, right in sorted(intervals):
            total += max(0.0, right - max(left, previous_end))
            previous_end = max(previous_end, right)
        coverage[speaker] = total
    return coverage


def _dominant_speaker(start: float, end: float, turns: list[dict[str, Any]]) -> str | None:
    """Return a speaker only when one turn clearly owns the cue."""
    if end <= start:
        return None
    coverage = _coverage_by_speaker(start, end, turns)
    if not coverage:
        return None
    ranked = sorted(coverage.items(), key=lambda item: item[1], reverse=True)
    best_speaker, best_overlap = ranked[0]
    second_overlap = ranked[1][1] if len(ranked) > 1 else 0.0
    duration = end - start
    if best_overlap < max(0.2, duration * 0.2):
        return None
    if best_overlap < sum(coverage.values()) * 0.65:
        return None
    if second_overlap and best_overlap - second_overlap < max(0.15, duration * 0.1):
        return None
    return best_speaker


def _word_speaker(start: float, end: float, turns: list[dict[str, Any]]) -> str | None:
    """Assign one word only when its local turn overlap is unambiguous."""
    if end <= start:
        return None
    coverage = _coverage_by_speaker(start, end, turns)
    if not coverage:
        return None
    ranked = sorted(coverage.items(), key=lambda item: item[1], reverse=True)
    best_speaker, best_overlap = ranked[0]
    second_overlap = ranked[1][1] if len(ranked) > 1 else 0.0
    duration = end - start
    if best_overlap < max(0.04, duration * 0.65):
        return None
    if second_overlap and best_overlap - second_overlap < max(0.03, duration * 0.15):
        return None
    return best_speaker


def _valid_words(segment: dict[str, Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for raw in segment.get("words") or []:
        try:
            word = str(raw["word"] or "").strip()
            start = float(raw["start"])
            end = float(raw["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if word and math.isfinite(start) and math.isfinite(end) and start < end:
            words.append({"word": word, "start": start, "end": end})
    return words


def _word_source_parts(source: str, words: list[dict[str, Any]]) -> list[str]:
    """Slice the original source so CJK spacing and punctuation survive splits."""
    text = str(source or "").strip()
    compact = "".join(text.split())
    word_compact = "".join("".join(str(item["word"]).split()) for item in words)
    if compact != word_compact:
        return [str(item["word"]).strip() for item in words]
    positions = [index for index, char in enumerate(text) if not char.isspace()]
    parts: list[str] = []
    source_cursor = 0
    compact_cursor = 0
    for item in words:
        length = len("".join(str(item["word"]).split()))
        stop = positions[compact_cursor + length - 1] + 1
        parts.append(text[source_cursor:stop])
        source_cursor = stop
        compact_cursor += length
    return parts


def split_segments_by_speaker(
    segments: list[dict[str, Any]], turns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Split word-timed cues at speaker changes before display timeline work."""
    result: list[dict[str, Any]] = []
    for segment in segments:
        words = _valid_words(segment)
        translation = str(segment.get("translation") or "").strip()
        source = str(segment.get("source") or "").strip()
        # A translated CapCut cue has no source-to-target word alignment.
        # Keep it atomic and use the conservative whole-cue assignment.
        if not words or (translation and translation != source):
            row = dict(segment)
            speaker = _dominant_speaker(float(row.get("start") or 0), float(row.get("end") or 0), turns)
            if speaker:
                row["speaker"] = speaker
            else:
                row.pop("speaker", None)
            result.append(row)
            continue

        labels = [_word_speaker(word["start"], word["end"], turns) for word in words]
        known = {label for label in labels if label}
        if len(known) == 1 and all(labels):
            row = dict(segment)
            speaker = next(iter(known), None) or _dominant_speaker(
                float(row.get("start") or 0), float(row.get("end") or 0), turns
            )
            if speaker:
                row["speaker"] = speaker
            else:
                row.pop("speaker", None)
            result.append(row)
            continue

        if not known:
            row = dict(segment)
            speaker = _dominant_speaker(
                float(row.get("start") or 0), float(row.get("end") or 0), turns
            )
            if speaker:
                row["speaker"] = speaker
            else:
                row.pop("speaker", None)
            result.append(row)
            continue

        parts = _word_source_parts(source, words)
        run_start = 0
        while run_start < len(words):
            label = labels[run_start]
            run_end = run_start + 1
            while run_end < len(words) and labels[run_end] == label:
                run_end += 1
            row = dict(segment)
            row["id"] = segment.get("id") if not result or result[-1].get("id") != segment.get("id") else uuid.uuid4().hex
            row["start"] = float(segment.get("start") or words[run_start]["start"]) if run_start == 0 else words[run_start]["start"]
            row["end"] = float(segment.get("end") or words[run_end - 1]["end"]) if run_end == len(words) else words[run_end - 1]["end"]
            row["words"] = words[run_start:run_end]
            row["source"] = "".join(parts[run_start:run_end]).strip()
            if translation == source:
                row["translation"] = row["source"]
            else:
                row["translation"] = ""
            if label:
                row["speaker"] = label
            else:
                row.pop("speaker", None)
                row["_speakerAmbiguous"] = True
            for field in ("audioFile", "audioUrl", "audioDuration"):
                row.pop(field, None)
            result.append(row)
            run_start = run_end

    for index, row in enumerate(result):
        row["index"] = index
    return result


def assign_speakers(segments: list[dict[str, Any]], turns: list[dict[str, Any]]) -> None:
    """Attach a speaker only when one turn clearly owns the whole cue.

    A transcript cue can span a turn change. Picking the first longest overlap
    assigns those mixed cues arbitrarily, so leave them unassigned for the
    editor instead of claiming the wrong speaker.
    """
    for segment in segments:
        segment.pop("speaker", None)
        start = float(segment.get("start") or 0)
        end = max(start, float(segment.get("end") or start))
        speaker = _dominant_speaker(start, end, turns)
        if speaker:
            segment["speaker"] = speaker


def diarization_provider_for_device(device: dict[str, Any]) -> str:
    """Map the shared ZM AI TOOL hardware result to a Sherpa provider."""
    accel = str(device.get("accel") or "").lower()
    gpu_kind = str(device.get("gpuKind") or "").lower()
    if accel == "cuda" or gpu_kind == "nvidia":
        return "cuda"
    if accel in ("metal", "coreml", "mps") or gpu_kind == "apple":
        return "coreml"
    # Sherpa's documented Python providers do not include DirectML.
    # AMD/Intel Windows therefore use the optimized multi-thread CPU path.
    return "cpu"


def preferred_diarization_provider() -> str:
    from pipeline.core.runtime_active import cpu_mode_enabled
    if cpu_mode_enabled():
        return 'cpu'
    forced = os.getenv("SPEAKER_DIARIZATION_PROVIDER", "auto").strip().lower()
    if forced in ("cpu", "cuda", "coreml"):
        return forced
    try:
        from pipeline.core.media import detect_device

        device = detect_device()
        return diarization_provider_for_device(device)
    except Exception:
        return "cpu"


def diarize_audio(
    audio_path: Path,
    model_dir: Path,
    num_speakers: int = 0,
    provider_out: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    try:
        from pipeline.core.cuda_dll import prefer_torch_cudnn

        prefer_torch_cudnn()
    except Exception:
        pass
    try:
        from pipeline.ocr.extract_parts.runtime import prepare_cuda_dlls

        prepare_cuda_dlls()
    except Exception:
        pass
    try:
        import numpy as np
        import sherpa_onnx
        import soundfile as sf
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Sherpa-ONNX chưa tải được native runtime. Vào Cấu hình → Thiết lập → "
            f"Cài tách người nói. Chi tiết: {exc}"
        ) from exc

    segmentation_env = os.getenv("SPEAKER_DIARIZATION_SEGMENTATION_MODEL", "").strip()
    embedding_env = os.getenv("SPEAKER_DIARIZATION_EMBEDDING_MODEL", "").strip()
    segmentation_path = Path(segmentation_env) if segmentation_env else model_dir / "model.int8.onnx"
    embedding_path = Path(embedding_env) if embedding_env else model_dir / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
    if not segmentation_env and not embedding_env:
        segmentation_path, embedding_path = ensure_diarization_models(model_dir)
    elif not segmentation_path.is_file() or not embedding_path.is_file():
        raise RuntimeError(f"Thiếu model diarization trong {model_dir}")

    samples, rate = sf.read(str(audio_path), dtype="float32", always_2d=True)
    samples = np.ascontiguousarray(samples[:, 0], dtype=np.float32)
    preferred = preferred_diarization_provider()
    providers = [preferred] if preferred == "cpu" else [preferred, "cpu"]
    last_error: Exception | None = None
    result = None
    for provider in providers:
        try:
            threads = 2 if provider in ("cuda", "coreml") else max(1, min(8, (os.cpu_count() or 4) - 1))
            segmentation = sherpa_onnx.OfflineSpeakerSegmentationModelConfig()
            segmentation.pyannote.model = str(segmentation_path)
            segmentation.provider = provider
            segmentation.num_threads = threads
            embedding = sherpa_onnx.SpeakerEmbeddingExtractorConfig()
            embedding.model = str(embedding_path)
            embedding.provider = provider
            embedding.num_threads = threads
            clustering = sherpa_onnx.FastClusteringConfig(
                # Auto stays deliberately conservative: a lower threshold
                # avoids merging two voices into one label. When the user
                # supplies a count, Sherpa uses the fixed number of clusters.
                num_clusters=num_speakers if num_speakers >= 2 else -1,
                threshold=_AUTO_CLUSTER_THRESHOLD,
            )
            config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=segmentation, embedding=embedding, clustering=clustering,
                min_duration_on=0.3, min_duration_off=0.5,
            )
            if not config.validate():
                raise RuntimeError(f"Sherpa provider {provider} không khả dụng")
            diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
            if int(rate) != int(diarizer.sample_rate):
                raise RuntimeError(f"Audio phải là {diarizer.sample_rate} Hz")
            result = diarizer.process(samples).sort_by_start_time()
            if provider_out is not None:
                provider_out["provider"] = provider
            break
        except Exception as exc:
            last_error = exc
    if result is None:
        raise RuntimeError(f"Không chạy được speaker diarization: {last_error}") from last_error
    return normalize_speaker_turns([
        {"start": float(turn.start), "end": float(turn.end), "speaker": f"SPEAKER_{int(turn.speaker):02d}"}
        for turn in result
    ])
