"""Domain API routes."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.deps import (
    AppConfigIn,
    CloneRenameIn,
    CompoundClipIn,
    ExportPayload,
    PreviewTtsIn,
    RebakeSpeedIn,
    RetranslateIn,
    SEG_PRESERVE,
    SegmentIn,
    Settings,
    StudioSynthIn,
    TextOverlayIn,
    UiPreferencesIn,
    VoiceBulkMoveIn,
    VoicePatchIn,
    require_meta,
    validate_overlay,
    validate_segment_editor_fields,
)
from api.job_spawn import spawn
from api.video_serve import serve_video_file
from pipeline import (
    DATA,
    PUBLIC_DATA,
    ensure_layout,
    ffprobe_duration,
    find_project_by_fp,
    hardware,
    list_voices,
    load_meta,
    mutate_meta,
    out_final,
    project_dir,
    request_cancel,
    run_dub,
    run_export,
    run_pipeline,
    save_meta,
    set_status,
    tts_cache_key,
    tts_segment,
    video_fingerprint,
)
from pipeline.core.jobs import arm_job
from pipeline.core.media import meta_baked_speed, meta_has_user_bake, video_size
from pipeline.export.mux import (
    export_project_audio,
    find_cached_no_vocals,
    read_stem_progress,
    separate_no_vocals,
)
from pipeline.tts import engines_status

router = APIRouter()

_install_state: dict[str, Any] = {
    "running": False,
    "kind": "",
    "message": "",
    "progress": 0,
    "error": "",
    "needsRestart": False,
    "result": None,
    "log": "",
    "startedAt": 0.0,
    "updatedAt": 0.0,
}
_install_lock = threading.Lock()
_checks_warm_lock = threading.Lock()
_checks_warming = False
_UPDATE_REPOSITORY = "manhgdev/zm_aio_tools"
_UPDATE_LOCK = threading.Lock()
_UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
_UPDATE_STATE: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "progress": 0,
    "message": "",
    "error": "",
    "assetName": "",
    "latestVersion": "",
    "packagePath": "",
}


def _release_version(value: str) -> str:
    normalized = str(value).strip()
    if normalized.startswith("action/"):
        normalized = normalized.removeprefix("action/")
    return normalized.removeprefix("v")


def _version_key(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", _release_version(value))
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def _desktop_version() -> str:
    return str(os.environ.get("ZM_AI_TOOL_VERSION") or "0.0.0").strip()


def _latest_release() -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{_UPDATE_REPOSITORY}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ZM-AIO-TOOL"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _desktop_platform_asset_suffix(platform_name: str | None = None, machine: str | None = None) -> str:
    """Return the only release asset suffix acceptable for this desktop build."""
    import platform as _platform
    platform_name = platform_name or sys.platform
    if machine is None:
        machine = _platform.machine().lower()  # works on all OS including Windows
    else:
        machine = machine.lower()
    if platform_name == "win32":
        return "-windows-x64.zip"
    if platform_name == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "-macos-arm64.pkg"
        if machine in {"x86_64", "amd64"}:
            return "-macos-x64.pkg"
    return ""


def _release_asset(release: dict[str, Any]) -> dict[str, Any] | None:
    suffix = _desktop_platform_asset_suffix()
    if not suffix:
        return None
    tag = str(release.get("tag_name") or "")
    version = _release_version(tag)
    if _version_key(tag) == (0, 0, 0):
        return None
    expected_names = (
        [f"ZM_AIO_TOOL_v{version}-windows-x64-Portable.zip", f"ZM_AIO_TOOL_v{version}-windows-x64.zip"]
        if suffix == "-windows-x64.zip"
        else [f"ZM_AIO_TOOL_v{version}{suffix}"]
    )
    assets = release.get("assets") or []
    for expected_name in expected_names:
        for asset in assets:
            if isinstance(asset, dict) and str(asset.get("name") or "") == expected_name:
                return asset
    return None


def _update_supported() -> bool:
    """The browser/dev server must never replace a local development checkout."""
    desktop = os.environ.get("ZM_AI_TOOL_DESKTOP")
    return desktop == "1" and bool(getattr(sys, "frozen", False))


def _update_snapshot() -> dict[str, Any]:
    with _UPDATE_LOCK:
        return dict(_UPDATE_STATE)


def _set_update_state(**values: Any) -> None:
    with _UPDATE_LOCK:
        _UPDATE_STATE.update(values)


def _download_update(asset: dict[str, Any], updates: Path, version: str) -> Path:
    name = str(asset.get("name") or "")
    url = str(asset.get("browser_download_url") or "")
    if not name or not url:
        raise RuntimeError("Release không có gói cài đặt phù hợp")
    # Asset names are supplied by GitHub; never allow a release response to
    # escape the dedicated update directory through a path separator.
    if Path(name).name != name or name in {".", ".."}:
        raise RuntimeError("Tên gói cập nhật không hợp lệ")
    try:
        expected_size = max(0, int(asset.get("size") or 0))
    except (TypeError, ValueError):
        expected_size = 0
    target = updates / name
    partial = target.with_suffix(target.suffix + ".part")
    _set_update_state(phase="downloading", progress=0, message="Đang tải bản cập nhật…", assetName=name, latestVersion=version)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            offset = partial.stat().st_size if partial.is_file() else 0
            request = urllib.request.Request(url, headers={"User-Agent": "ZM-AIO-TOOL"})
            if offset:
                request.add_header("Range", f"bytes={offset}-")
            # Large desktop bundles can take several minutes on a slow connection;
            # this is a socket-idle timeout, not a total-download deadline.
            with urllib.request.urlopen(request, timeout=_UPDATE_DOWNLOAD_TIMEOUT_SECONDS) as response:
                if offset and response.status != 206:
                    offset = 0
                    partial.unlink(missing_ok=True)
                with partial.open("ab" if offset else "wb") as output:
                    try:
                        content_range = str(response.headers.get("Content-Range") or "")
                        total = int(content_range.rsplit("/", 1)[1]) if "/" in content_range else int(response.headers.get("Content-Length") or 0) + offset
                    except (TypeError, ValueError, IndexError):
                        total = 0
                    received = offset
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        received += len(chunk)
                        progress = min(99, int(received * 100 / total)) if total else 0
                        _set_update_state(progress=progress)
            partial.replace(target)
            actual_size = target.stat().st_size
            if expected_size and actual_size != expected_size:
                target.unlink(missing_ok=True)
                raise OSError(
                    f"Gói cập nhật tải chưa đủ ({actual_size}/{expected_size} byte)"
                )
            last_error = None
            break
        except (BrokenPipeError, ConnectionError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < 2:
                if expected_size and partial.is_file() and partial.stat().st_size > expected_size:
                    partial.unlink(missing_ok=True)
                _set_update_state(message=f"Kết nối gián đoạn, đang thử lại ({attempt + 2}/3)…")
                time.sleep(0.5 * (attempt + 1))
            else:
                partial.unlink(missing_ok=True)
                raise
    if last_error is not None:
        raise last_error
    _set_update_state(progress=100)
    return target


def _windows_update_script(updates: Path) -> Path:
    script = updates / "apply-update.ps1"
    script.write_text(
        """# ZM AIO TOOL Windows Auto-Updater (PowerShell 5.1+ compatible)
# Params duoc doc tu update-params.json cung thu muc voi script
# (tranh quoting issues khi truyen qua command line)
param([string]$ParamsFile)

$ErrorActionPreference = 'Stop'

# Doc params tu JSON
$paramsPath = if ($ParamsFile) { $ParamsFile } else { Join-Path $PSScriptRoot 'update-params.json' }
$p         = Get-Content -LiteralPath $paramsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$AppPid    = [int]$p.AppPid
$Zip       = [string]$p.Zip
$Target    = [string]$p.Target
$Exe       = [string]$p.Exe
$ReadyFile = [string]$p.ReadyFile

$LogFile = Join-Path (Split-Path $Zip -Parent) 'update.log'

function Log($msg) {
    $time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$time] $msg" | Out-File -FilePath $LogFile -Append -Encoding utf8
}

Log "=== Bat dau cap nhat ZM AIO TOOL portable ==="
Log "AppPid:    $AppPid"
Log "Zip:       $Zip"
Log "Target:    $Target"
Log "Exe:       $Exe"
Log "ReadyFile: $ReadyFile"

try {
    # 1. Cho process goi cap nhat thoat
    if ($AppPid -gt 0) {
        Log "Cho process $AppPid thoat..."
        Wait-Process -Id $AppPid -Timeout 10 -ErrorAction SilentlyContinue
    }

    # 2. Dong cac process dang chay tu thu muc cu
    $killDir = $Target
    $exeName  = [System.IO.Path]::GetFileName($Exe)
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($Exe)
    Log "Dong process $baseName trong $killDir..."
    for ($i = 0; $i -lt 10; $i++) {
        $procs = Get-Process -Name "$baseName" -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -and $_.Path.StartsWith($killDir, [System.StringComparison]::OrdinalIgnoreCase) } catch { $false }
        }
        if (-not $procs) { break }
        Log "Dang dong $($procs.Count) process..."
        $procs | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Milliseconds 800

    # 3. Giai nen zip vao thu muc temp (trong cung thu muc chua zip)
    $updateDir = Split-Path $Zip -Parent
    $stamp     = Get-Date -Format 'yyyyMMddHHmmss'
    $tmpDir    = Join-Path $updateDir ('extracted-' + $stamp)
    Log "Giai nen vao temp: $tmpDir"
    if (Test-Path $tmpDir) { Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue }
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        $extractRoot = [System.IO.Path]::GetFullPath($tmpDir + [System.IO.Path]::DirectorySeparatorChar)
        foreach ($entry in $archive.Entries) {
            $entryPath = [System.IO.Path]::GetFullPath((Join-Path $tmpDir $entry.FullName))
            if (-not $entryPath.StartsWith($extractRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Goi cap nhat chua duong dan khong an toan: $($entry.FullName)"
            }
        }
    } finally {
        $archive.Dispose()
    }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $tmpDir)

    # Tim thu muc goc chua EXE (zip phang hoac zip co subfolder)
    $sourceDir = $tmpDir
    if (-not (Test-Path (Join-Path $sourceDir $exeName))) {
        $sub = Get-ChildItem -LiteralPath $tmpDir -Directory | Where-Object { Test-Path (Join-Path $_.FullName $exeName) } | Select-Object -First 1
        if ($sub) { $sourceDir = $sub.FullName; Log "sourceDir trong subfolder: $sourceDir" }
    }
    if (-not (Test-Path (Join-Path $sourceDir $exeName))) {
        throw "Goi cap nhat khong hop le: Thieu $exeName trong ban giai nen."
    }

    # Go Zone.Identifier (MOTW) khoi tat ca file moi
    Get-ChildItem -LiteralPath $sourceDir -Recurse -File | ForEach-Object {
        Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue
    }
    Log "Da go Zone.Identifier."

    # 4. Chi thay payload bat bien. data/output/runtime/resources/logs luon duoc giu nguyen.
    $backup = Join-Path $updateDir ('backup-' + $stamp)
    $payloadNames = @($exeName, 'app', '_internal')
    $newPayloadNames = @($exeName)
    foreach ($name in @('app', '_internal')) {
        if (Test-Path -LiteralPath (Join-Path $sourceDir $name)) { $newPayloadNames += $name }
    }
    $installedNames = New-Object System.Collections.Generic.List[string]
    $committed = $false

    function Move-WithRetry([string]$Source, [string]$Destination) {
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            try {
                Move-Item -LiteralPath $Source -Destination $Destination -Force -ErrorAction Stop
                return
            } catch {
                Log "Move lan $attempt ($Source -> $Destination): $_"
                Start-Sleep -Milliseconds 600
            }
        }
        throw "Khong the di chuyen $Source vao $Destination."
    }

    function Restore-Backup() {
        foreach ($name in $installedNames) {
            $current = Join-Path $Target $name
            if (Test-Path -LiteralPath $current) {
                Remove-Item -LiteralPath $current -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        foreach ($name in $payloadNames) {
            $saved = Join-Path $backup $name
            if (Test-Path -LiteralPath $saved) {
                Move-WithRetry $saved (Join-Path $Target $name)
            }
        }
    }

    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    foreach ($name in $payloadNames) {
        $old = Join-Path $Target $name
        if (Test-Path -LiteralPath $old) {
            Move-WithRetry $old (Join-Path $backup $name)
        }
    }
    foreach ($name in $newPayloadNames) {
        $source = Join-Path $sourceDir $name
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Goi cap nhat thieu payload: $name"
        }
        Move-WithRetry $source (Join-Path $Target $name)
        $installedNames.Add($name)
    }

    # 5. Khoi dong va giu backup den khi app moi song qua giai doan bootstrap.
    $newExe = Join-Path $Target $exeName
    Log "Launch: $newExe"
    Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue
    $env:ZM_AI_TOOL_UPDATE_READY_FILE = $ReadyFile
    $env:ZM_AI_TOOL_SUPERVISOR_CHILD = $null
    $newProcess = Start-Process -FilePath $newExe -WorkingDirectory $Target -PassThru
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $ReadyFile -PathType Leaf)) {
        if ($newProcess.HasExited) {
            throw "Ban moi thoat som voi ma $($newProcess.ExitCode); se khoi phuc ban cu."
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Path -LiteralPath $ReadyFile -PathType Leaf)) {
        Stop-Process -Id $newProcess.Id -Force -ErrorAction SilentlyContinue
        throw "Ban moi khong bao san sang sau 90 giay; se khoi phuc ban cu."
    }
    Start-Sleep -Seconds 3
    if ($newProcess.HasExited) {
        throw "Ban moi thoat sau khi khoi dong voi ma $($newProcess.ExitCode); se khoi phuc ban cu."
    }
    $env:ZM_AI_TOOL_UPDATE_READY_FILE = $null
    Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue
    $committed = $true
    Log "=== Cap nhat thanh cong; du lieu portable duoc giu nguyen. ==="
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Zip -Force -ErrorAction SilentlyContinue

} catch {
    $err = $_.Exception.Message
    Log "LOI CAP NHAT: $err"
    $env:ZM_AI_TOOL_UPDATE_READY_FILE = $null
    if ($ReadyFile) { Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue }
    if (-not $committed -and $backup -and (Test-Path -LiteralPath $backup)) {
        try {
            Get-Process -Name "$baseName" -ErrorAction SilentlyContinue | Where-Object {
                try { $_.Path -and $_.Path.StartsWith($Target, [System.StringComparison]::OrdinalIgnoreCase) } catch { $false }
            } | Stop-Process -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 800
            Restore-Backup
            $oldExe = Join-Path $Target $exeName
            if (Test-Path -LiteralPath $oldExe -PathType Leaf) {
                Start-Process -FilePath $oldExe -WorkingDirectory $Target
                Log "Da khoi phuc va mo lai ban cu."
            }
        } catch {
            Log "LOI KHOI PHUC: $_"
        }
    } elseif (Test-Path -LiteralPath (Join-Path $Target $exeName) -PathType Leaf) {
        Start-Process -FilePath (Join-Path $Target $exeName) -WorkingDirectory $Target -ErrorAction SilentlyContinue
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show("Cap nhat that bai / Update failed:`n$err`n`nLog: $LogFile", "Loi cap nhat / Update error - ZM AIO TOOL", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
    } catch {}
}
""",
        encoding="utf-8-sig",
    )
    return script



def _start_checks_warm() -> None:
    """Populate the first-run cache once; polling requests must not spawn a thread each."""
    global _checks_warming
    with _checks_warm_lock:
        if _checks_warming:
            return
        _checks_warming = True

    def work() -> None:
        global _checks_warming
        try:
            from pipeline.core.system_check import system_checks
            from pipeline.core.system_check.checks import _invalidate_checks_cache

            _invalidate_checks_cache()  # Xoá cache cũ để re-check thấy trạng thái mới
            system_checks(fast=True)
        except Exception as exc:
            # A broken optional probe must never produce an unhandled daemon
            # thread traceback or take down the API; the next refresh can retry.
            try:
                from pipeline.core.app_log import append_exception

                append_exception("[warm-checks] dependency check failed", exc)
            except Exception:
                pass
        finally:
            with _checks_warm_lock:
                _checks_warming = False

    threading.Thread(target=work, name="warm-checks", daemon=True).start()


def _append_install_log(text: str) -> None:
    """Thread-safe append to install log, keep last 200 lines."""
    with _install_lock:
        lines = [*_install_state["log"].splitlines(), *text.splitlines()]
        _install_state["log"] = "\n".join(lines[-200:])
        _install_state["updatedAt"] = time.time()


def _set_install_progress(progress: int | float, message: str = "") -> None:
    """Publish monotonic, backend-owned install progress to polling clients."""
    try:
        value = max(0, min(99, round(float(progress))))
    except (TypeError, ValueError):
        return
    with _install_lock:
        if not _install_state["running"]:
            return
        current = int(_install_state.get("progress") or 0)
        if value < current:
            return
        _install_state["progress"] = value
        if message:
            _install_state["message"] = message
        _install_state["updatedAt"] = time.time()


def _setup_gate_path() -> Path:
    home = (os.environ.get("ZM_AI_TOOL_HOME") or "").strip()
    if home:
        return Path(home) / "setup_ok"
    return Path(DATA) / "setup_ok"


def _setup_gate_passed() -> bool:
    return _setup_gate_path().is_file()


def _mark_setup_gate() -> None:
    path = _setup_gate_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n", encoding="utf-8")


def _start_install_job(kind: str, fn, *, needs_restart: bool = True) -> dict[str, Any]:
    started_at = time.time()
    with _install_lock:
        if _install_state["running"]:
            return {
                "ok": True,
                "running": True,
                "kind": _install_state["kind"],
                "message": _install_state["message"],
                "progress": _install_state["progress"],
            }
        _install_state.update(
            running=True,
            kind=kind,
            message="Đang chuẩn bị cài đặt… / Preparing installation…",
            progress=1,
            error="",
            needsRestart=False,
            result=None,
            log="",
            startedAt=started_at,
            updatedAt=started_at,
        )

    def work() -> None:
        import pipeline.core.system_check as _sc
        _sc._install_log_fn = _append_install_log
        _sc._install_progress_fn = _set_install_progress
        try:
            result = fn()
            changed = "Đã cài" in str(result.get("message", ""))
            desktop = os.environ.get("ZM_AI_TOOL_DESKTOP")
            if changed and needs_restart and desktop == "1":
                result = {**result, "needsRestart": True}
            with _install_lock:
                _install_state["result"] = result
                _install_state["message"] = str(result.get("message") or "")
                _install_state["progress"] = 100
                _install_state["needsRestart"] = bool(result.get("needsRestart"))
                _install_state["updatedAt"] = time.time()
        except Exception as e:
            with _install_lock:
                _install_state["error"] = str(e)
                _install_state["message"] = "Cài đặt thất bại / Installation failed"
                _install_state["updatedAt"] = time.time()
        finally:
            _sc._install_log_fn = None
            _sc._install_progress_fn = None
            with _install_lock:
                _install_state["running"] = False

        _start_checks_warm()

    threading.Thread(target=work, name=f"install-{kind}", daemon=True).start()
    return {
        "ok": True,
        "running": True,
        "kind": kind,
        "message": "Đang chuẩn bị cài đặt… / Preparing installation…",
        "progress": 1,
    }

# Aliases matching original routes_all names
_spawn = spawn
_serve_video_file = serve_video_file
_validate_overlay = validate_overlay
_validate_segment_editor_fields = validate_segment_editor_fields
_SEG_PRESERVE = SEG_PRESERVE

try:
    from pipeline.core.config import load_app_config, save_app_config
except Exception:  # pragma: no cover
    load_app_config = save_app_config = None  # type: ignore
try:
    from pipeline.core import system_check
except Exception:  # pragma: no cover
    system_check = None  # type: ignore


@router.get("/api/hardware")
def api_hardware():
    return hardware()


def _clamp_percent(value: object) -> int | None:
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return None


def _apple_gpu_percent(raw: str) -> int | None:
    match = re.search(r'"Device Utilization %"\s*=\s*(\d+)', raw)
    return _clamp_percent(match.group(1)) if match else None


def _gpu_percent() -> int | None:
    if sys.platform == "darwin":
        try:
            raw = subprocess.check_output(
                ["ioreg", "-r", "-d1", "-c", "IOAccelerator"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            return _apple_gpu_percent(raw)
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        raw = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
        return _clamp_percent(raw.splitlines()[0].strip())
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


@router.get("/api/hardware/usage")
def api_hardware_usage():
    try:
        import psutil  # type: ignore

        cpu = _clamp_percent(psutil.cpu_percent(interval=0.05))
    except Exception:
        try:
            cpu = _clamp_percent((os.getloadavg()[0] / max(1, os.cpu_count() or 1)) * 100)
        except (AttributeError, OSError):
            cpu = None
    return {"cpuPercent": cpu, "gpuPercent": _gpu_percent()}


@router.get("/api/config")
def api_get_config():
    from pipeline.core.app_config import public_app_config

    return public_app_config()


@router.post("/api/system/resolve-output-folder")
def api_resolve_output_folder(tab: str = "flow", output: str = ""):
    """Resolve a feature output name to the exact desktop folder used by workers."""
    from pipeline.core.output_paths import selected_or_default

    return {"path": str(selected_or_default(tab, output))}


@router.post("/api/config")
def api_save_config(body: AppConfigIn):
    from pipeline.core.app_config import public_app_config, save_app_config

    patch: dict = {"cloud": {}}
    if body.cloud:
        for k, v in body.cloud.items():
            block = {
                "baseUrl": v.baseUrl or "",
                "model": v.model or "",
            }
            if v.keys is not None:
                block["keys"] = v.keys
            if v.apiKeys is not None:
                block["apiKeys"] = v.apiKeys
            elif v.apiKey is not None:
                block["apiKey"] = v.apiKey
            patch["cloud"][k] = block
    if body.tts and body.tts.elevenlabs is not None:
        patch["tts"] = {
            "elevenlabs": {
                "apiKeys": body.tts.elevenlabs.apiKeys
                if body.tts.elevenlabs.apiKeys is not None
                else "",
                "keys": body.tts.elevenlabs.keys,
            }
        }
    save_app_config(patch)
    # Provider discovery is cached briefly; credentials saved in this request
    # must be visible to the Chat/Automation provider picker immediately.
    try:
        from api.routes.chat import service as chat_service

        chat_service.clear_model_cache()
    except Exception:
        pass
    # key ElevenLabs đổi → xóa cache list giọng (tránh kẹt [] từ lần trước chưa có key)
    try:
        from pipeline.tts.eleven import clear_el_voices_cache

        clear_el_voices_cache()
    except Exception:
        pass
    return public_app_config()


@router.get("/api/ui-preferences")
def api_get_ui_preferences():
    from pipeline.core.ui_preferences import load_ui_preferences

    return load_ui_preferences()


@router.put("/api/ui-preferences")
def api_save_ui_preferences(body: UiPreferencesIn):
    from pathlib import Path as _Path

    from pipeline.core.output_paths import ensure_writable_output_root
    from pipeline.core.ui_preferences import save_ui_preferences

    output_root = None
    if body.outputRoot is not None:
        raw = body.outputRoot.strip()
        if raw and not _Path(raw).is_absolute():
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="outputRoot must be an absolute path")
        if raw:
            try:
                raw = str(ensure_writable_output_root(_Path(raw)))
            except OSError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Không thể ghi vào thư mục đầu ra đã chọn. / "
                        f"The selected output folder is not writable: {exc}"
                    ),
                ) from exc
        output_root = raw  # empty string = reset
    return save_ui_preferences(locale=body.locale, storage=body.storage, output_root=output_root)


@router.get("/api/system/checks")
def api_system_checks(refresh: bool = False, deep: bool = False):
    """Dependency checklist. Trả loading:true ngay nếu cache chưa có — không block request."""
    from pipeline.core import system_check as _sc

    try:
        checks_module = _sc.checks
        with _install_lock:
            installing = _install_state.get("running", False)

        # Cache có rồi — trả ngay, không tính toán gì thêm.
        if not refresh and not installing:
            with checks_module._checks_lock:
                cached = checks_module._CHECKS_CACHE
            if cached is not None:
                return cached[2]

        # Cache rỗng HOẶC install đang chạy — trả loading ngay,
        # background thread (warm-checks) sẽ điền cache.
        if checks_module._CHECKS_CACHE is None:
            _start_checks_warm()
            return {"items": [], "loading": True, "device": {}}

        return _sc.system_checks(refresh=refresh and not installing, fast=True)
    except Exception as e:
        raise HTTPException(500, f"system checks failed: {e}") from e


@router.get("/api/resources")
def api_resources():
    """Unified view for optional AI runtimes/models; no project media here."""
    from importlib.util import find_spec
    from pipeline.core.config import DATA
    from pipeline.core.accel import local_ai_runtime_profile

    diarization = Path(DATA) / "models" / "pyannote"
    runtime = local_ai_runtime_profile()

    # Frozen app: sherpa_onnx nằm trong .venv-runtime → find_spec() không thấy.
    # Dùng _runtime_mod_ok (subprocess import) hoặc kiểm tra dist-info trực tiếp.
    if getattr(sys, "frozen", False):
        from pipeline.core.system_check.probe import _runtime_mod_ok, _mod_ok_fast
        sherpa_ok = _mod_ok_fast("sherpa_onnx")[0] or _runtime_mod_ok("sherpa_onnx")[0]
        whisper_ok = _mod_ok_fast("faster_whisper")[0] or _runtime_mod_ok("faster_whisper")[0]
        ocr_ok = _mod_ok_fast("rapidocr_onnxruntime")[0] or _runtime_mod_ok("rapidocr_onnxruntime")[0]
    else:
        sherpa_ok = find_spec("sherpa_onnx") is not None
        whisper_ok = find_spec("faster_whisper") is not None
        ocr_ok = find_spec("rapidocr_onnxruntime") is not None

    diarization_installed = sherpa_ok and (diarization / "model.int8.onnx").is_file()
    resources = [
        {"id": "whisper", "name": "Whisper", "kind": "asr", "installed": whisper_ok, "provider": runtime["label"], "action": "ai_runtime"},
        {"id": "diarization", "name": "Sherpa-ONNX (Tách người nói)", "kind": "diarization", "installed": diarization_installed, "provider": runtime["label"], "action": "ai_runtime"},
        {"id": "ocr", "name": "RapidOCR", "kind": "ocr", "installed": ocr_ok, "provider": runtime["label"], "action": "ai_runtime"},
    ]
    return {"items": resources}


@router.post("/api/resources/{resource_id}/install")
def api_install_resource(resource_id: str):
    if resource_id in {"whisper", "diarization", "ocr"}:
        if resource_id == "diarization":
            # Kiểm tra nếu models chưa có thì phải force cài thực sự
            # (không bị frozen fast-path bỏ qua bước download model)
            from pipeline.core.config import DATA
            diarization_dir = Path(DATA) / "models" / "pyannote"
            models_ok = (diarization_dir / "model.int8.onnx").is_file()
            if not models_ok:
                from pipeline.core.system_check import install_ai_runtime
                return _start_install_job("ai_runtime", install_ai_runtime)
        return api_install_ai_runtime()
    raise HTTPException(404, "Resource không tồn tại")



@router.post("/api/system/ollama/signin")
def api_ollama_signin():
    """Mở luồng đăng nhập chính chủ; ZM AI TOOL không đọc hay giữ token Ollama."""
    import subprocess

    from pipeline.core.system_check.checks import _ollama_executable

    exe = _ollama_executable()
    if not exe:
        raise HTTPException(404, "Chưa tìm thấy Ollama trên máy")
    try:
        subprocess.Popen(
            [exe, "signin"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                if sys.platform == "win32"
                else 0
            ),
        )
    except OSError as e:
        raise HTTPException(500, f"Không mở được Ollama Sign in: {e}") from e
    return {"ok": True, "message": "Đã mở Ollama Sign in"}


@router.get("/api/system/install/status")
def api_install_status():
    with _install_lock:
        st = dict(_install_state)
    out: dict[str, Any] = {
        "running": bool(st.get("running")),
        "kind": st.get("kind") or "",
        "progress": int(st.get("progress") or 0),
        "message": st.get("message") or "",
        "startedAt": float(st.get("startedAt") or 0),
        "updatedAt": float(st.get("updatedAt") or 0),
    }
    if st.get("log"):
        # Trả 30 dòng cuối để tránh payload quá lớn.
        out["log"] = "\n".join(st["log"].splitlines()[-30:])
    if st.get("error"):
        out["error"] = st["error"]
        out["ok"] = False
        return out
    if st.get("result") and not st.get("running"):
        result = st["result"] if isinstance(st["result"], dict) else {}
        out.update(result)
        out["running"] = False
        if st.get("needsRestart"):
            out["needsRestart"] = True
        return out
    return out


@router.post("/api/system/install/ai_runtime")
def api_install_ai_runtime():
    from pipeline.core.system_check import install_ai_runtime

    # Only the background import/CUDA probe can declare a runtime ready.
    # dist-info files survive broken installs and CPU-only torch upgrades.
    return _start_install_job("ai_runtime", install_ai_runtime)



@router.post("/api/system/install/ocr_cuda")
def api_install_ocr_cuda():
    from pipeline.core.system_check import install_ocr_cuda

    return _start_install_job("ocr_cuda", install_ocr_cuda)


@router.post("/api/system/install/demucs_cuda")
def api_install_demucs_cuda():
    from pipeline.core.system_check import install_demucs_cuda

    return _start_install_job("demucs_cuda", install_demucs_cuda, needs_restart=False)


@router.post("/api/system/install/nvm")
def api_install_nvm():
    from pipeline.core.system_check import install_nvm

    return _start_install_job("nvm", install_nvm, needs_restart=False)


@router.get("/api/system/setup-gate")
def api_get_setup_gate():
    """Cổng first-run — lưu file dưới ZM_AI_TOOL_HOME (không phụ thuộc port/localStorage)."""
    return {"passed": _setup_gate_passed()}


@router.post("/api/system/setup-gate")
def api_pass_setup_gate():
    _mark_setup_gate()
    return {"passed": True}


@router.post("/api/system/restart")
def api_system_restart():
    """Khởi động lại bản desktop — gọi sau khi cài xong mọi gói cần reload."""
    if (os.environ.get("ZM_AI_TOOL_DESKTOP")) != "1":
        raise HTTPException(400, "Chỉ bản desktop hỗ trợ khởi động lại từ app")
    subprocess.Popen([sys.executable, "--restart-after", str(os.getpid())])
    threading.Timer(0.8, lambda: os._exit(0)).start()
    return {"ok": True, "message": "Đang khởi động lại…"}


@router.get("/api/system/update/check")
def api_update_check():
    if (os.environ.get("ZM_AI_TOOL_DESKTOP")) != "1":
        return {"desktop": False, "supported": False, "updateAvailable": False, "currentVersion": _desktop_version()}
    try:
        release = _latest_release()
        tag = str(release.get("tag_name") or "")
        asset = _release_asset(release)
        version = _release_version(tag)
        return {
            "desktop": True,
            "supported": _update_supported(),
            "currentVersion": _desktop_version(),
            "latestVersion": version,
            "releaseAvailable": _version_key(tag) > _version_key(_desktop_version()),
            "updateAvailable": bool(asset and _version_key(tag) > _version_key(_desktop_version())),
            "assetAvailable": bool(asset),
            "assetName": str(asset.get("name") or "") if asset else "",
            "releaseUrl": str(release.get("html_url") or ""),
            "notes": str(release.get("body") or ""),
        }
    except Exception as exc:
        raise HTTPException(502, f"Không kiểm tra được GitHub Release: {exc}") from exc


@router.post("/api/system/update/install")
def api_update_install():
    if not _update_supported():
        raise HTTPException(400, "Chỉ bản desktop hỗ trợ cập nhật")
    with _UPDATE_LOCK:
        if _UPDATE_STATE["running"]:
            return {"ok": True, "running": True, "message": _UPDATE_STATE["message"]}
        _UPDATE_STATE.update(running=True, phase="checking", progress=0, message="Đang chuẩn bị cập nhật…", error="", packagePath="")

    def work() -> None:
        try:
            release = _latest_release()
            tag = str(release.get("tag_name") or "")
            version = _release_version(tag)
            asset = _release_asset(release)
            if not asset or _version_key(tag) <= _version_key(_desktop_version()):
                _set_update_state(phase="complete", progress=100, message="Đã là phiên bản mới nhất")
                return
            if sys.platform == "win32" and getattr(sys, "frozen", False):
                exe_dir = Path(sys.executable).resolve().parent
                probe = exe_dir / f".zmaio-update-write-{uuid.uuid4().hex}.tmp"
                try:
                    probe.open("x").close()
                    probe.unlink()
                except OSError as exc:
                    try:
                        probe.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise RuntimeError(
                        "Thư mục ứng dụng không cho phép cập nhật. Hãy chuyển toàn bộ thư mục "
                        "Portable sang nơi có quyền ghi (ví dụ C:\\ZM_AIO_TOOL) rồi thử lại. / "
                        "The app folder is not writable. Move the complete Portable folder to a "
                        "writable location (for example C:\\ZM_AIO_TOOL) and retry."
                    ) from exc
                updates = Path(os.environ.get("ZM_AI_TOOL_HOME") or DATA) / "updates"
            else:
                updates = Path(os.environ.get("ZM_AI_TOOL_HOME") or DATA) / "updates"
            updates.mkdir(parents=True, exist_ok=True)
            package = _download_update(asset, updates, version)
            _set_update_state(phase="ready", progress=100, message="Đã tải gói cập nhật", packagePath=str(package))
        except Exception as exc:
            _set_update_state(phase="error", error=str(exc), message="Không thể tải bản cập nhật")
        finally:
            _set_update_state(running=False)

    threading.Thread(target=work, name="desktop-update-download", daemon=True).start()
    return {"ok": True, "running": True, "message": "Đang tải bản cập nhật…"}


@router.get("/api/system/update/status")
def api_update_status():
    desktop = os.environ.get("ZM_AI_TOOL_DESKTOP")
    return {"desktop": desktop == "1", **_update_snapshot()}


def _launch_windows_updater(package: Path) -> None:
    """Start the detached staged updater; it waits for this app before swapping."""
    exe = Path(sys.executable).resolve()
    old_target = exe.parent
    target = old_target
    script = _windows_update_script(package.parent)
    params = package.parent / "update-params.json"
    ready = package.parent / f"update-ready-{uuid.uuid4().hex}.txt"
    ready.unlink(missing_ok=True)
    params.write_text(
        json.dumps(
            {
                "AppPid": os.getpid(),
                "Zip": str(package.resolve()),
                "Target": str(target.resolve()),
                "Exe": exe.name,
                "ReadyFile": str(ready.resolve()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    flags = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-ParamsFile",
            str(params),
        ],
        cwd=str(package.parent),
        creationflags=flags,
        close_fds=True,
    )


@router.post("/api/system/update/apply")
def api_update_apply():
    if not _update_supported():
        raise HTTPException(400, "Chỉ bản desktop hỗ trợ cập nhật")
    state = _update_snapshot()
    if state["running"]:
        raise HTTPException(409, "Gói cập nhật vẫn đang tải")
    if state["phase"] != "ready" or not state["packagePath"]:
        raise HTTPException(400, "Chưa có gói cập nhật đã tải")
    package = Path(str(state["packagePath"]))
    if not package.is_file():
        raise HTTPException(404, "Không tìm thấy gói cập nhật đã tải")
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(package)])
        _set_update_state(phase="complete", message="Đã mở macOS Installer để cập nhật")
        return {"ok": True, "message": "Đã mở macOS Installer để cập nhật"}

    try:
        _launch_windows_updater(package)
    except Exception as exc:
        _set_update_state(phase="error", error=str(exc), message="Không thể mở trình cập nhật")
        raise HTTPException(500, f"Không thể mở trình cập nhật: {exc}") from exc
    _set_update_state(phase="applying", progress=100, message="Đang đóng app để cập nhật và mở lại…")
    threading.Timer(0.8, lambda: os._exit(0)).start()
    return {"ok": True, "message": "Đang đóng app để cập nhật và mở lại…"}



@router.get("/api/system/logs")
def api_system_logs(tail: int = 800):
    """Log app (job lỗi, crash hook) — tab Cấu hình → Log."""
    from pipeline.core.app_log import read_log

    try:
        return read_log(tail=tail)
    except Exception as e:
        raise HTTPException(500, f"log read failed: {e}") from e


@router.delete("/api/system/logs")
def api_system_logs_clear():
    from pipeline.core.app_log import clear_log

    return clear_log()


def _windows_native_dialog(script: str, extra_env: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    env.update(extra_env or {})
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Sta", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=300,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"PowerShell kết thúc với mã {result.returncode}")
    return result.stdout.strip()


def _macos_native_dialog(script: str) -> str:
    """Run an AppleScript picker without requiring Python's optional Tk build."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode:
        # Cancelling a macOS picker is normal UI flow, not an API failure.
        message = result.stderr.strip()
        if "User canceled" in message or "user canceled" in message:
            return ""
        raise RuntimeError(message or f"osascript kết thúc với mã {result.returncode}")
    return result.stdout.strip()


def _apple_script_string(value: str) -> str:
    """Quote a trusted dialog string for use as an AppleScript literal."""
    return json.dumps(value, ensure_ascii=False)


def _pick_folder(title: str) -> str:
    if os.name == "nt":
        return _windows_native_dialog(
            """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Width = 1
$owner.Height = 1
$owner.Show()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $env:VIDEOCLONE_DIALOG_TITLE
$dialog.ShowNewFolderButton = $true
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.SelectedPath)
}
$owner.Close()
""",
            {"VIDEOCLONE_DIALOG_TITLE": title},
        )
    if sys.platform == "darwin":
        return _macos_native_dialog(
            f"return POSIX path of (choose folder with prompt {_apple_script_string(title)})"
        )
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        return filedialog.askdirectory(title=title)
    finally:
        root.destroy()


def _pick_file(title: str, file_filter: str) -> str:
    if os.name == "nt":
        return _windows_native_dialog(
            """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Width = 1
$owner.Height = 1
$owner.Show()
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = $env:VIDEOCLONE_DIALOG_TITLE
$dialog.Filter = $env:VIDEOCLONE_FILE_FILTER
$dialog.Multiselect = $false
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.FileName)
}
$owner.Close()
""",
            {
                "VIDEOCLONE_DIALOG_TITLE": title,
                "VIDEOCLONE_FILE_FILTER": file_filter,
            },
        )
    if sys.platform == "darwin":
        return _macos_native_dialog(
            f"return POSIX path of (choose file with prompt {_apple_script_string(title)})"
        )
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        return filedialog.askopenfilename(title=title)
    finally:
        root.destroy()


@router.post("/api/system/pick-srt-image-file")
def api_pick_srt_image_file(kind: str):
    choices = {
        "audio": ("Chọn file audio", "Audio|*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg|Tất cả tệp|*.*"),
        "timeline": ("Chọn file timeline", "Timeline|*.txt;*.srt;*.vtt;*.ass;*.ssa;*.csv;*.tsv;*.json;*.lrc|Tất cả tệp|*.*"),
        "srt": ("Chọn file phụ đề", "Phụ đề SRT|*.srt|Tất cả tệp|*.*"),
        "watermark": ("Chọn ảnh logo", "Ảnh|*.png;*.jpg;*.jpeg;*.jfif;*.webp;*.bmp|Tất cả tệp|*.*"),
    }
    if kind not in choices:
        raise HTTPException(400, "Loại file không hợp lệ")
    try:
        selected = _pick_file(*choices[kind])
        return {"ok": bool(selected), "path": str(Path(selected).resolve()) if selected else ""}
    except Exception as exc:
        raise HTTPException(500, f"Không mở được hộp thoại chọn file: {exc}") from exc


@router.post("/api/system/pick-videos")
def api_pick_videos():
    """Chọn một hoặc nhiều file video (Clone/Review batch)."""
    title = "Chọn video"
    filt = "Video|*.mp4;*.mov;*.mkv;*.webm;*.avi;*.m4v|Tất cả tệp|*.*"
    try:
        if os.name == "nt":
            raw = _windows_native_dialog(
                """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Width = 1
$owner.Height = 1
$owner.Show()
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = $env:VIDEOCLONE_DIALOG_TITLE
$dialog.Filter = $env:VIDEOCLONE_FILE_FILTER
$dialog.Multiselect = $true
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write(($dialog.FileNames -join [Environment]::NewLine))
}
$owner.Close()
""",
                {"VIDEOCLONE_DIALOG_TITLE": title, "VIDEOCLONE_FILE_FILTER": filt},
            )
        elif sys.platform == "darwin":
            raw = _macos_native_dialog(
                "set theFiles to choose file with prompt "
                f"{_apple_script_string(title)} "
                "of type {\"public.movie\", \"public.mpeg-4\"} with multiple selections allowed\n"
                "set out to \"\"\n"
                "repeat with f in theFiles\n"
                "set out to out & (POSIX path of f) & linefeed\n"
                "end repeat\n"
                "return out"
            )
        else:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            try:
                root.withdraw()
                root.attributes("-topmost", True)
                picked = filedialog.askopenfilenames(title=title)
                raw = "\n".join(picked)
            finally:
                root.destroy()
        paths = [str(Path(p.strip()).resolve()) for p in (raw or "").splitlines() if p.strip()]
        return {"ok": bool(paths), "paths": paths}
    except Exception as exc:
        raise HTTPException(500, f"Không mở được hộp thoại chọn video: {exc}") from exc


@router.post("/api/system/pick-folder")
def api_pick_folder():
    """Mở native folder picker dialog, trả về path user chọn."""
    try:
        folder = _pick_folder("Chọn thư mục xuất")
        return {"ok": bool(folder), "path": str(Path(folder).resolve()) if folder else ""}
    except Exception as e:
        raise HTTPException(500, f"Không mở được folder dialog: {e}") from e


@router.post("/api/system/pick-save-video")
def api_pick_save_video(filename: str = "ghep-anh-video-srt.mp4"):
    """Mở native Save As dialog và trả về đường dẫn MP4 đầy đủ."""
    try:
        from pipeline.core.output_paths import downloads_folder

        initial = f"{Path(filename).stem or 'ghep-anh-video-srt'}.mp4"
        initial_dir = downloads_folder("subtitle-image")
        if os.name == "nt":
            path = _windows_native_dialog(
                """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Width = 1
$owner.Height = 1
$owner.Show()
$dialog = New-Object System.Windows.Forms.SaveFileDialog
$dialog.Title = 'Chọn nơi lưu video'
$dialog.Filter = 'Video MP4 (*.mp4)|*.mp4|Tất cả tệp (*.*)|*.*'
$dialog.DefaultExt = 'mp4'
$dialog.AddExtension = $true
$dialog.FileName = $env:VIDEOCLONE_SAVE_NAME
$dialog.InitialDirectory = $env:VIDEOCLONE_SAVE_DIR
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.FileName)
}
$owner.Close()
""",
                {"VIDEOCLONE_SAVE_NAME": initial, "VIDEOCLONE_SAVE_DIR": str(initial_dir)},
            )
        elif sys.platform == "darwin":
            path = _macos_native_dialog(
                "set outputFile to choose file name with prompt "
                f"{_apple_script_string('Chọn nơi lưu video')} "
                f"default name {_apple_script_string(initial)} "
                f"default location (POSIX file {_apple_script_string(str(initial_dir))})\n"
                "return POSIX path of outputFile"
            )
        else:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            try:
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.asksaveasfilename(
                    title="Chọn nơi lưu video", defaultextension=".mp4",
                    initialdir=str(initial_dir),
                    initialfile=initial,
                    filetypes=[("Video MP4", "*.mp4"), ("Tất cả tệp", "*.*")],
                )
            finally:
                root.destroy()
        if not path:
            return {"ok": False, "path": ""}
        selected = Path(path).resolve()
        if selected.suffix.lower() != ".mp4":
            selected = selected.with_suffix(".mp4")
        return {"ok": True, "path": str(selected)}
    except Exception as e:
        raise HTTPException(500, f"Không mở được hộp thoại lưu video: {e}") from e


@router.post("/api/system/pick-media-folder")
def api_pick_media_folder():
    """Chọn thư mục chứa ảnh/video đầu vào."""
    try:
        folder = _pick_folder("Chọn thư mục ảnh / video")
        return {"ok": bool(folder), "path": str(Path(folder).resolve()) if folder else ""}
    except Exception as e:
        raise HTTPException(500, f"Không mở được hộp thoại chọn thư mục media: {e}") from e
