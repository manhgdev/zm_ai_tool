"""Windows portable state selection and non-destructive version migration."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path


_VERSIONED_APP = re.compile(
    r"^ZM[_ ]AIO[_ ]TOOL[_ ]?v(\d+)\.(\d+)\.(\d+)(?:-|$)", re.IGNORECASE
)
_APP_EXECUTABLES = ("ZM AI TOOL.exe",)
_INSTALLED_MARKER = ".zmaio-installed"
_LEGACY_STATE: tuple[tuple[str, str], ...] = (
    ("data", "data"),
    ("public_data", "data/public"),
    (".python-runtime", "data/.python-runtime"),
    ("capcut_device.json", "data/capcut_device.json"),
    ("output", "output"),
    (".venv-runtime", ".venv-runtime"),
    (".venv-ocr", ".venv-ocr"),
    ("resources", "resources"),
    ("app.log", "app.log"),
    ("last_crash.txt", "last_crash.txt"),
)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def ensure_writable_directory(folder: Path) -> Path:
    """Create *folder* and prove that files can be created and removed there."""
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".zmaio-write-", dir=folder):
        pass
    return folder


def windows_portable_home(
    executable: Path, environ: Mapping[str, str] | None = None
) -> tuple[Path, Path | None]:
    """Select stable state storage for Windows Setup and Portable builds.

    Inno Setup writes ``.zmaio-installed`` next to the EXE, so an installed
    copy always uses LocalAppData even when it was launched elevated or placed
    in a custom writable folder. A marker-free Portable copy remains truly
    portable and stores state beside the EXE whenever that location is writable.
    """
    env = os.environ if environ is None else environ
    # Keep mapped/subst drive spelling; resolving junctions can make AI paths much longer.
    portable_root = executable.absolute().parent
    local = Path(
        env.get("LOCALAPPDATA")
        or (Path.home() / "AppData" / "Local")
    )
    if (portable_root / _INSTALLED_MARKER).is_file():
        installed_home = local / "ZM_AI_TOOL"
        return ensure_writable_directory(installed_home), portable_root
    try:
        return ensure_writable_directory(portable_root), None
    except OSError:
        fallback = local / "ZM_AI_TOOL" / "portable-data"
        return ensure_writable_directory(fallback), portable_root


def _version_key(folder: Path) -> tuple[int, int, int]:
    match = _VERSIONED_APP.match(folder.name)
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def _legacy_homes(
    executable: Path, home: Path, environ: Mapping[str, str]
) -> list[tuple[Path, bool]]:
    """Return the current read-only root and older ZM AI TOOL siblings."""
    executable_dir = executable.absolute().parent
    confirmed_old_target: Path | None = None
    for params in (
        executable_dir.parent / "update-params.json",
        executable_dir / "updates" / "update-params.json",
    ):
        try:
            payload = json.loads(params.read_text(encoding="utf-8-sig"))
            old_target = str(payload.get("OldTarget") or "").strip()
            if old_target and _same_path(Path(str(payload.get("Target") or "")), executable_dir):
                confirmed_old_target = Path(old_target)
                break
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            pass
    siblings: list[Path] = []
    current_version = _version_key(executable_dir)
    try:
        for candidate in executable_dir.parent.iterdir():
            candidate_version = _version_key(candidate)
            if (
                candidate.is_dir()
                and not _same_path(candidate, executable_dir)
                and candidate_version != (0, 0, 0)
                and (current_version == (0, 0, 0) or candidate_version < current_version)
                and any((candidate / name).is_file() for name in _APP_EXECUTABLES)
            ):
                siblings.append(candidate)
    except OSError:
        pass
    siblings.sort(key=_version_key, reverse=True)

    # Only move when the updater explicitly identifies its old target.
    # A manually extracted second copy must not steal state from another copy.
    result: list[tuple[Path, bool]] = []
    if not _same_path(executable_dir, home):
        result.append((executable_dir, False))
    result += [
        (candidate, confirmed_old_target is not None and _same_path(candidate, confirmed_old_target))
        for candidate in siblings
    ]
    return result


def _transfer(source: Path, target: Path, *, move: bool) -> None:
    """Atomically expose a migrated entry; retain the source if copying is needed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        try:
            source.replace(target)
            return
        except OSError:
            pass

    staged = target.with_name(f".{target.name}.migrating-{os.getpid()}")
    if staged.exists():
        if staged.is_dir():
            shutil.rmtree(staged)
        else:
            staged.unlink()
    try:
        if source.is_dir():
            shutil.copytree(source, staged)
        else:
            shutil.copy2(source, staged)
        staged.replace(target)
    except Exception:
        if staged.is_dir():
            shutil.rmtree(staged, ignore_errors=True)
        else:
            staged.unlink(missing_ok=True)
        raise


def migrate_windows_state(
    home: Path,
    executable: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[Path], list[str], list[str]]:
    """Move missing state from an older release without overwriting current data."""
    env = os.environ if environ is None else environ
    sources = _legacy_homes(executable, home, env)
    used: list[Path] = []
    migrated: list[str] = []
    errors: list[str] = []
    for source_name, target_name in _LEGACY_STATE:
        target = home.joinpath(*target_name.split("/"))
        if target.exists():
            continue
        for source_home, move in sources:
            source = source_home / source_name
            if not source.exists():
                continue
            try:
                _transfer(source, target, move=move)
                migrated.append(f"{source} -> {target}")
                if not any(_same_path(source_home, item) for item in used):
                    used.append(source_home)
            except Exception as exc:
                errors.append(f"{source}: {exc}")
            break
    return used, migrated, errors


def _rebase_saved_output_root(preferences: Path, old_home: Path, home: Path) -> bool:
    try:
        payload = json.loads(preferences.read_text(encoding="utf-8"))
        raw = str(payload.get("outputRoot") or "").strip()
        relative = Path(raw).absolute().relative_to((old_home / "output").absolute())
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return False
    payload["outputRoot"] = str(home / "output" / relative)
    staged = preferences.with_name(f".{preferences.name}.migrating-{os.getpid()}")
    try:
        staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        staged.replace(preferences)
    except OSError:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def sync_windows_portable_root(
    home: Path, migrated_from: Sequence[Path] = ()
) -> list[Path]:
    """Remember moves and rebase only paths that belonged to the old default output."""
    marker = home / "data" / ".portable-root"
    previous: list[Path] = []
    try:
        saved = marker.read_text(encoding="utf-8").strip()
        if saved:
            try:
                payload = json.loads(saved)
                if isinstance(payload, dict):
                    current = str(payload.get("current") or "").strip()
                    if current:
                        previous.append(Path(current))
                    history = payload.get("previous")
                    for item in history if isinstance(history, list) else []:
                        if str(item).strip():
                            previous.append(Path(str(item)))
                else:
                    previous.append(Path(saved))
            except (ValueError, TypeError, json.JSONDecodeError):
                previous.append(Path(saved))
    except OSError:
        pass
    for source in migrated_from:
        if not any(_same_path(source, item) for item in previous):
            previous.append(source)
    unique: list[Path] = []
    for path in previous:
        if not _same_path(path, home) and not any(_same_path(path, item) for item in unique):
            unique.append(path)
    previous = unique[:8]

    preferences = home / "data" / "ui_preferences.json"
    for old_home in previous:
        if (old_home / "output").exists() and not (home / "output").exists():
            continue
        if _rebase_saved_output_root(preferences, old_home, home):
            break

    staged = marker.with_name(f".{marker.name}.tmp-{os.getpid()}")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(
            json.dumps(
                {"current": str(home.absolute()), "previous": [str(path) for path in previous]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        staged.replace(marker)
    except OSError:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
    return previous
