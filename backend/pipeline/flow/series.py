"""Persistent Series workspace for Flow character and scene continuity."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from pipeline.core.output_paths import downloads_folder, safe_output_part
from pipeline.core.config import PUBLIC_DATA
from . import store


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_SERIES_RE = re.compile(r"^\s*#\s*SERIES\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EPISODE_RE = re.compile(r"^\s*#\s*TẬP\s*(\d+)\s*(?:[—-]\s*(.+?))?\s*$", re.IGNORECASE)
_SCENE_RE = re.compile(r"^\s*(\d{1,3})_\[([^\]]+)\]\s*(.+?)\s*$")


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _series_folder(series_id: str) -> Path:
    folder = store.root() / "series" / safe_output_part(series_id, "series")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _asset_folder(series_id: str) -> Path:
    folder = _series_folder(series_id) / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _rows() -> list[dict[str, Any]]:
    return [_normalize(item) for item in store.list_rows("series")]


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    """Keep older Series rows usable as fields are added over time."""
    normalized = dict(item)
    normalized.setdefault("description", "")
    normalized.setdefault("bible", "")
    normalized.setdefault("slug", safe_output_part(str(normalized.get("title") or "series"), "series"))
    normalized.setdefault("anchorAssets", [])
    normalized.setdefault("assets", [])
    normalized.setdefault("episodes", [])
    return normalized


def list_series() -> list[dict[str, Any]]:
    return sorted(_rows(), key=lambda item: float(item.get("updatedAt") or 0), reverse=True)


def get_series(series_id: str) -> dict[str, Any] | None:
    item = store.get_row("series", series_id)
    return _normalize(item) if item else None


def _scene_defaults(index: int, prompt: str = "", timecode: str = "") -> dict[str, Any]:
    return {
        "id": _id("scene"),
        "index": index,
        "title": f"Cảnh {index:03d}",
        "prompt": prompt,
        "timecode": timecode,
        "status": "draft",
        "continuityEnabled": True,
        "referenceAssetIds": [],
        "promptOverride": "",
        "keyframeJobId": "",
        "keyframeOutput": "",
        "approvedKeyframe": "",
        "videoJobId": "",
        "videoOutput": "",
        "endFrame": "",
        "error": "",
    }


def _episode_defaults(index: int, title: str = "") -> dict[str, Any]:
    return {
        "id": _id("episode"),
        "index": index,
        "title": title or f"Tập {index:02d}",
        "state": "",
        "scenes": [],
    }


def create_series(title: str, bible: str = "", description: str = "") -> dict[str, Any]:
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("Series title is required")
    now = _now()
    item = {
        "id": _id("series"),
        "title": clean_title[:160],
        "slug": safe_output_part(clean_title, "series"),
        "description": str(description or "")[:2000],
        "bible": str(bible or "")[:12000],
        "anchorAssets": [],
        "assets": [],
        "episodes": [],
        "createdAt": now,
        "updatedAt": now,
    }
    store.put_row("series", item)
    _series_folder(str(item["id"]))
    return item


def update_series(series_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    series = get_series(series_id)
    if not series:
        return None
    allowed = {"title", "description", "bible", "anchorAssets", "assets", "episodes"}
    changed = {key: value for key, value in patch.items() if key in allowed}
    if "title" in changed:
        title = str(changed["title"] or "").strip()
        if not title:
            raise ValueError("Series title is required")
        changed["title"] = title[:160]
        # Output paths must remain stable when users rename a Series.
    if "description" in changed:
        changed["description"] = str(changed["description"] or "")[:2000]
    if "bible" in changed:
        changed["bible"] = str(changed["bible"] or "")[:12000]
    if "anchorAssets" in changed:
        values = [str(value) for value in changed["anchorAssets"] if str(value)][:3]
        changed["anchorAssets"] = values
    changed["updatedAt"] = _now()
    return store.patch_row("series", series_id, changed)


def delete_series(series_id: str) -> bool:
    series = get_series(series_id)
    if not series:
        return False
    removed = store.delete_row("series", series_id)
    if removed:
        shutil.rmtree(_series_folder(series_id), ignore_errors=True)
        slug = safe_output_part(str(series.get("slug") or series.get("title") or "series"), "series")
        # Remove the current dedicated Series root plus legacy pre-3.8 paths.
        # Never touch ordinary Flow jobs or output belonging to another Series.
        for root in (downloads_folder("flow"), PUBLIC_DATA / "flow"):
            shutil.rmtree(root / "series" / slug, ignore_errors=True)
            for kind in ("image", "video"):
                shutil.rmtree(root / kind / slug, ignore_errors=True)
    return removed


def create_episode(series_id: str, title: str = "", state: str = "") -> dict[str, Any] | None:
    series = get_series(series_id)
    if not series:
        return None
    episodes = [dict(item) for item in series.get("episodes") or []]
    episode = _episode_defaults(len(episodes) + 1, str(title or "").strip())
    episode["state"] = str(state or "")[:5000]
    episodes.append(episode)
    update_series(series_id, {"episodes": episodes})
    return episode


def _find_episode(series: dict[str, Any], episode_id: str) -> tuple[int, dict[str, Any]] | tuple[None, None]:
    for index, episode in enumerate(series.get("episodes") or []):
        if str(episode.get("id")) == episode_id:
            return index, dict(episode)
    return None, None


def _save_episode(series_id: str, episode_index: int, episode: dict[str, Any]) -> dict[str, Any] | None:
    series = get_series(series_id)
    if not series:
        return None
    episodes = [dict(item) for item in series.get("episodes") or []]
    if episode_index < 0 or episode_index >= len(episodes):
        return None
    episodes[episode_index] = episode
    return update_series(series_id, {"episodes": episodes})


def update_episode(series_id: str, episode_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    series = get_series(series_id)
    if not series:
        return None
    index, episode = _find_episode(series, episode_id)
    if episode is None or index is None:
        return None
    for key, limit in (("title", 160), ("state", 5000)):
        if key in patch:
            episode[key] = str(patch[key] or "")[:limit]
    _save_episode(series_id, index, episode)
    return episode


def delete_episode(series_id: str, episode_id: str) -> bool:
    series = get_series(series_id)
    if not series:
        return False
    episodes = [dict(item) for item in series.get("episodes") or []]
    kept = [episode for episode in episodes if str(episode.get("id")) != episode_id]
    if len(kept) == len(episodes):
        return False
    for index, episode in enumerate(kept, 1):
        episode["index"] = index
    update_series(series_id, {"episodes": kept})
    return True


def create_scene(series_id: str, episode_id: str, prompt: str = "", timecode: str = "") -> dict[str, Any] | None:
    series = get_series(series_id)
    if not series:
        return None
    index, episode = _find_episode(series, episode_id)
    if episode is None or index is None:
        return None
    scenes = [dict(item) for item in episode.get("scenes") or []]
    scene = _scene_defaults(len(scenes) + 1, str(prompt or "")[:12000], str(timecode or "")[:120])
    scenes.append(scene)
    episode["scenes"] = scenes
    _save_episode(series_id, index, episode)
    return scene


def _find_scene(series: dict[str, Any], episode_id: str, scene_id: str) -> tuple[int, int, dict[str, Any], dict[str, Any]] | tuple[None, None, None, None]:
    episode_index, episode = _find_episode(series, episode_id)
    if episode is None or episode_index is None:
        return None, None, None, None
    for scene_index, scene in enumerate(episode.get("scenes") or []):
        if str(scene.get("id")) == scene_id:
            return episode_index, scene_index, episode, dict(scene)
    return None, None, None, None


def update_scene(series_id: str, episode_id: str, scene_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    series = get_series(series_id)
    if not series:
        return None
    episode_index, scene_index, episode, scene = _find_scene(series, episode_id, scene_id)
    if episode is None or scene is None or episode_index is None or scene_index is None:
        return None
    limits = {
        "title": 160, "prompt": 12000, "timecode": 120, "promptOverride": 12000,
        "error": 4000, "status": 64, "keyframeJobId": 120, "keyframeOutput": 2000,
        "approvedKeyframe": 2000, "videoJobId": 120, "videoOutput": 2000, "endFrame": 2000,
    }
    for key, limit in limits.items():
        if key in patch:
            scene[key] = str(patch[key] or "")[:limit]
    if "continuityEnabled" in patch:
        scene["continuityEnabled"] = bool(patch["continuityEnabled"])
    if "referenceAssetIds" in patch:
        scene["referenceAssetIds"] = [str(value) for value in patch["referenceAssetIds"] if str(value)][:3]
    scenes = [dict(item) for item in episode.get("scenes") or []]
    scenes[scene_index] = scene
    episode["scenes"] = scenes
    _save_episode(series_id, episode_index, episode)
    return scene


def delete_scene(series_id: str, episode_id: str, scene_id: str) -> bool:
    series = get_series(series_id)
    if not series:
        return False
    episode_index, _scene_index, episode, _scene = _find_scene(series, episode_id, scene_id)
    if episode is None or episode_index is None:
        return False
    kept = [dict(item) for item in episode.get("scenes") or [] if str(item.get("id")) != scene_id]
    if len(kept) == len(episode.get("scenes") or []):
        return False
    for index, scene in enumerate(kept, 1):
        scene["index"] = index
    episode["scenes"] = kept
    _save_episode(series_id, episode_index, episode)
    return True


def import_script(text: str) -> dict[str, Any]:
    title = ""
    episodes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = []
    for number, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line == "---":
            continue
        series_match = _SERIES_RE.match(line)
        if series_match:
            title = series_match.group(1).strip()
            continue
        episode_match = _EPISODE_RE.match(line)
        if episode_match:
            current = _episode_defaults(int(episode_match.group(1)), (episode_match.group(2) or "").strip())
            episodes.append(current)
            continue
        scene_match = _SCENE_RE.match(line)
        if scene_match and current is not None:
            scene = _scene_defaults(int(scene_match.group(1)), scene_match.group(3).strip(), scene_match.group(2).strip())
            current["scenes"].append(scene)
            continue
        errors.append({"line": number, "text": raw_line, "message": "Invalid series script line"})
    if not title:
        errors.append({"line": 1, "text": "", "message": "Missing # SERIES: title"})
    if not episodes:
        errors.append({"line": 1, "text": "", "message": "Missing # TẬP heading"})
    if any(not episode["scenes"] for episode in episodes):
        errors.extend({"line": 0, "text": episode["title"], "message": "Episode has no scenes"} for episode in episodes if not episode["scenes"])
    if errors:
        return {"ok": False, "errors": errors}
    for episode_position, episode in enumerate(episodes, 1):
        episode["index"] = episode_position
        for scene_position, scene in enumerate(episode["scenes"], 1):
            scene["index"] = scene_position
    return {"ok": True, "title": title, "episodes": episodes}


def create_from_script(text: str, bible: str = "") -> dict[str, Any]:
    parsed = import_script(text)
    if not parsed.get("ok"):
        return parsed
    series = create_series(str(parsed["title"]), bible=bible)
    saved = update_series(str(series["id"]), {"episodes": parsed["episodes"]})
    return {"ok": True, "series": saved}


def add_asset(series_id: str, filename: str, content: bytes, *, label: str = "", locked: bool = True) -> dict[str, Any]:
    series = get_series(series_id)
    if not series:
        raise KeyError(series_id)
    suffix = Path(filename or "anchor.png").suffix.lower()
    if suffix not in _IMAGE_EXTENSIONS:
        raise ValueError("Only PNG, JPG, JPEG, and WebP reference images are supported")
    asset_id = _id("asset")
    target = _asset_folder(series_id) / f"{asset_id}{suffix}"
    target.write_bytes(content)
    asset = {"id": asset_id, "name": Path(filename or target.name).name, "label": str(label or "")[:120], "locked": bool(locked), "path": str(target), "createdAt": _now()}
    assets = [dict(item) for item in series.get("assets") or []]
    assets.append(asset)
    update_series(series_id, {"assets": assets})
    return asset


def delete_asset(series_id: str, asset_id: str) -> bool:
    series = get_series(series_id)
    if not series:
        return False
    assets = [dict(item) for item in series.get("assets") or []]
    asset = next((item for item in assets if str(item.get("id")) == asset_id), None)
    if not asset:
        return False
    Path(str(asset.get("path") or "")).unlink(missing_ok=True)
    update_series(series_id, {
        "assets": [item for item in assets if str(item.get("id")) != asset_id],
        "anchorAssets": [item for item in series.get("anchorAssets") or [] if item != asset_id],
    })
    return True


def update_asset(series_id: str, asset_id: str, *, label: str | None = None, locked: bool | None = None) -> dict[str, Any] | None:
    series = get_series(series_id)
    if not series:
        return None
    assets = [dict(item) for item in series.get("assets") or []]
    asset = next((item for item in assets if str(item.get("id")) == asset_id), None)
    if not asset:
        return None
    if label is not None:
        asset["label"] = str(label)[:120]
    if locked is not None:
        asset["locked"] = bool(locked)
    update_series(series_id, {"assets": assets})
    return asset


def asset_path(series_id: str, asset_id: str) -> Path | None:
    series = get_series(series_id)
    if not series:
        return None
    asset = next((item for item in series.get("assets") or [] if str(item.get("id")) == asset_id), None)
    path = Path(str((asset or {}).get("path") or ""))
    return path if path.is_file() else None


def _previous_scene(series: dict[str, Any], episode_id: str, scene_id: str) -> dict[str, Any] | None:
    """Prior scene inside the same episode only (film continuity, not cross-episode)."""
    _episode_index, episode = _find_episode(series, episode_id)
    if episode is None:
        return None
    scenes = list(episode.get("scenes") or [])
    current_index = next(
        (index for index, scene in enumerate(scenes) if str(scene.get("id")) == scene_id),
        -1,
    )
    return dict(scenes[current_index - 1]) if current_index > 0 else None


def _asset_paths(series: dict[str, Any], ids: list[str]) -> list[str]:
    by_id = {str(item.get("id")): str(item.get("path")) for item in series.get("assets") or []}
    return [path for asset_id in ids if (path := by_id.get(str(asset_id))) and Path(path).is_file()]


def extract_video_end_frame(video: Path, dest: Path) -> bool:
    """Grab the last frame of a Series clip without forcing a portrait canvas.

    Older builds scaled to 768x1376 (9:16), which warped landscape Tom&Jerry-style
    films and broke continuity into the next Veo Frames shot.
    """
    video = Path(video)
    dest = Path(dest)
    if not video.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".tmp.png")
    try:
        # Keep aspect; cap long edge so Flow upload stays light.
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-sseof", "-0.08", "-i", str(video),
                "-vf", "scale='min(1280,iw)':-2",
                "-frames:v", "1", str(temp),
            ],
            check=True,
            timeout=60,
        )
        if not temp.is_file() or temp.stat().st_size < 64:
            temp.unlink(missing_ok=True)
            return False
        temp.replace(dest)
        return dest.is_file()
    except (OSError, subprocess.SubprocessError):
        temp.unlink(missing_ok=True)
        return False


def ensure_scene_end_frame(series_id: str, scene: dict[str, Any], *, episode_id: str = "") -> Path | None:
    """Return an on-disk end frame, extracting from videoOutput when missing."""
    existing = Path(str(scene.get("endFrame") or ""))
    if existing.is_file():
        return existing
    video = Path(str(scene.get("videoOutput") or ""))
    if not video.is_file():
        return None
    dest = _asset_folder(series_id) / f"{str(scene.get('id') or 'scene')}_end.png"
    if not extract_video_end_frame(video, dest):
        return None
    ep_id = episode_id or _episode_id_for_scene(series_id, str(scene.get("id") or "")) or ""
    scene_id = str(scene.get("id") or "")
    if ep_id and scene_id:
        update_scene(series_id, ep_id, scene_id, {"endFrame": str(dest)})
    return dest if dest.is_file() else None


def _episode_id_for_scene(series_id: str, scene_id: str) -> str | None:
    series = get_series(series_id)
    if not series:
        return None
    for episode in series.get("episodes") or []:
        for scene in episode.get("scenes") or []:
            if str(scene.get("id")) == scene_id:
                return str(episode.get("id") or "")
    return None


def _locked_anchor_paths(series: dict[str, Any]) -> list[str]:
    """Character/prop lock images (anchor order, locked preferred)."""
    anchor_ids = [str(value) for value in series.get("anchorAssets") or []]
    locked_ids = [
        str(asset.get("id")) for asset in series.get("assets") or []
        if asset.get("locked") and str(asset.get("id")) in anchor_ids
    ]
    ordered = list(dict.fromkeys([*locked_ids, *anchor_ids]))
    return _asset_paths(series, ordered)[:3]


def generation_context(series_id: str, episode_id: str, scene_id: str, artifact: str, prompt_override: str = "") -> dict[str, Any]:
    series = get_series(series_id)
    if not series:
        raise KeyError("Series not found")
    episode_index, _scene_index, episode, scene = _find_scene(series, episode_id, scene_id)
    if scene is None or episode is None or episode_index is None:
        raise KeyError("Episode or scene not found")
    if artifact not in {"keyframe", "video"}:
        raise ValueError("Unsupported Series artifact")
    scene_number = int(scene.get("index") or 1)
    previous = _previous_scene(series, episode_id, scene_id)
    previous_end: Path | None = None
    if previous and bool(scene.get("continuityEnabled", True)):
        previous_end = ensure_scene_end_frame(series_id, previous, episode_id=episode_id)
        if previous_end is None:
            series = get_series(series_id) or series
            previous = _previous_scene(series, episode_id, scene_id) or previous
            candidate = Path(str((previous or {}).get("endFrame") or ""))
            previous_end = candidate if candidate.is_file() else None
    continuation = (
        artifact == "video"
        and bool(scene.get("continuityEnabled", True))
        and previous is not None
        and previous_end is not None
        and previous_end.is_file()
    )
    locked_labels = [
        str(asset.get("label") or asset.get("name") or "").strip()
        for asset in series.get("assets") or []
        if asset.get("locked") and str(asset.get("id")) in {str(v) for v in series.get("anchorAssets") or []}
    ]
    locked_labels = [label for label in locked_labels if label][:3]
    lock_line = (
        f"LOCKED CHARACTERS/PROPS (keep identical): {', '.join(locked_labels)}."
        if locked_labels else
        "Keep recurring character appearance, wardrobe, and art style identical across shots."
    )
    prompt_parts = [
        str(series.get("bible") or "").strip(),
        str(episode.get("state") or "").strip(),
        lock_line,
        (
            f"SHOT {scene_number}: render only this scene's stated action. "
            "Do not repeat a previous scene, skip ahead, redesign locked characters, "
            "or introduce a realistic human. Begin from this shot's START STATE and "
            "finish on its END STATE so the next shot can continue seamlessly."
        ),
        str(scene.get("prompt") or "").strip(),
        str(prompt_override or scene.get("promptOverride") or "").strip(),
    ]
    if continuation:
        # Visual start frame carries look/world; keep lock names + action only so
        # Veo does not redraw characters from a long bible.
        prompt_parts = [
            (
                "Continue the exact preceding video from the provided start frame. "
                "Identical character appearance, wardrobe, proportions, world, lighting, and camera language. "
                "Do not restart the story, do not reintroduce characters, do not reset the set."
            ),
            lock_line,
            str(scene.get("prompt") or "").strip(),
            str(prompt_override or scene.get("promptOverride") or "").strip(),
        ]
    prompt = "\n\n".join(part for part in prompt_parts if part)
    if not prompt:
        raise ValueError("Scene prompt is required")
    slug = safe_output_part(series.get("slug") or series.get("title") or "series", "series")
    output_dir = f"{slug}/tap-{int(episode.get('index') or 1):02d}"
    context = {
        "seriesId": series_id,
        "episodeId": episode_id,
        "sceneId": scene_id,
        "artifact": artifact,
        "seriesTitle": series.get("title") or "",
        "seriesSlug": series.get("slug") or safe_output_part(str(series.get("title") or "series"), "series"),
        "episodeIndex": int(episode.get("index") or 1),
        "sceneIndex": int(scene.get("index") or 1),
        "outputDir": output_dir,
        "prompt": prompt,
        "continuity": continuation,
    }
    if artifact == "video":
        if continuation and previous_end is not None:
            # Must start from prior final frame — never a freshly drifted keyframe.
            context["sourceFiles"] = [str(previous_end)]
            return context
        if previous and bool(scene.get("continuityEnabled", True)) and previous_end is None:
            raise ValueError(
                "SERIES_CONTINUITY_MISSING_END_FRAME: hoàn thành video cảnh trước (có end frame) "
                "trước khi nối cảnh này / finish the previous scene video first"
            )
        keyframe = Path(str(scene.get("approvedKeyframe") or ""))
        if keyframe.is_file():
            context["sourceFiles"] = [str(keyframe)]
            return context
        # First shot fallback: locked character anchors as start stills.
        anchors = _locked_anchor_paths(series)
        context["sourceFiles"] = anchors[:1]
        return context
    # Keyframe: previous end frame (continuity) + locked character anchors.
    source_paths: list[str] = []
    if previous_end is not None and previous_end.is_file():
        source_paths.append(str(previous_end))
    requested_ids = [str(value) for value in scene.get("referenceAssetIds") or []]
    anchor_ids = [str(value) for value in series.get("anchorAssets") or []]
    locked_ids = [
        str(asset.get("id")) for asset in series.get("assets") or []
        if asset.get("locked") and str(asset.get("id")) in anchor_ids
    ]
    reference_ids = list(dict.fromkeys([*locked_ids, *requested_ids, *anchor_ids]))[:3]
    source_paths.extend(_asset_paths(series, reference_ids))
    context["sourceFiles"] = list(dict.fromkeys(source_paths))[:3]
    return context


def register_job(job: dict[str, Any]) -> None:
    context = job.get("seriesContext") or {}
    if not context:
        return
    artifact = str(context.get("artifact") or "")
    if artifact == "anchor":
        return
    patch = {
        "status": "generating_keyframe" if artifact == "keyframe" else "generating_video",
        "error": "",
        "keyframeJobId" if artifact == "keyframe" else "videoJobId": str(job.get("id") or ""),
    }
    update_scene(str(context.get("seriesId") or ""), str(context.get("episodeId") or ""), str(context.get("sceneId") or ""), patch)


def mark_job_complete(job: dict[str, Any], outputs: list[str]) -> None:
    context = job.get("seriesContext") or {}
    if not context or not outputs:
        return
    artifact = str(context.get("artifact") or "")
    series_id = str(context.get("seriesId") or "")
    if artifact == "anchor":
        source = Path(str(outputs[0]))
        if source.is_file():
            asset = add_asset(series_id, source.name, source.read_bytes(), label=str(context.get("anchorLabel") or "AI anchor"), locked=True)
            item = get_series(series_id)
            if item and asset["id"] not in item.get("anchorAssets", []):
                update_series(series_id, {"anchorAssets": [*item.get("anchorAssets", []), asset["id"]][:3]})
        return
    episode_id = str(context.get("episodeId") or "")
    scene_id = str(context.get("sceneId") or "")
    if artifact == "keyframe":
        update_scene(series_id, episode_id, scene_id, {"status": "awaiting_keyframe", "keyframeOutput": str(outputs[0]), "error": ""})
        return
    if artifact == "video":
        video = Path(str(outputs[0]))
        end_frame = _asset_folder(series_id) / f"{scene_id}_end.png"
        end_frame_value = str(end_frame) if extract_video_end_frame(video, end_frame) else ""
        update_scene(series_id, episode_id, scene_id, {"status": "complete", "videoOutput": str(video), "endFrame": end_frame_value, "error": ""})


def mark_job_error(job: dict[str, Any], error: str) -> None:
    context = job.get("seriesContext") or {}
    if str(context.get("artifact") or "") == "anchor":
        return
    if context:
        update_scene(str(context.get("seriesId") or ""), str(context.get("episodeId") or ""), str(context.get("sceneId") or ""), {"status": "error", "error": str(error)[:4000]})


def approve_keyframe(series_id: str, episode_id: str, scene_id: str, source: str) -> dict[str, Any] | None:
    series = get_series(series_id)
    if not series:
        return None
    _episode_index, _scene_index, _episode, scene = _find_scene(series, episode_id, scene_id)
    if scene is None:
        return None
    original = Path(source)
    if not original.is_file():
        raise ValueError("Keyframe output is missing")
    target = _asset_folder(series_id) / f"{scene_id}_keyframe{original.suffix.lower() or '.png'}"
    shutil.copy2(original, target)
    return update_scene(series_id, episode_id, scene_id, {"approvedKeyframe": str(target), "status": "ready_video", "error": ""})
