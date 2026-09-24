"""Domain API routes."""
from __future__ import annotations

import json
import hashlib
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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "stage": "",
    "runtimePack": "",
    "runtimeProfile": "",
    "currentPackage": "",
    "downloadedBytes": 0,
    "totalBytes": 0,
    "requiredDiskBytes": 0,
    "errorCode": "",
    "retryable": False,
    "diagnostics": "",
    "speedBytesPerSecond": None,
    "etaSeconds": None,
    "cacheHit": 0,
    "downloadItems": [],
    "freeDiskBytes": None,
    "diskDrive": "",
    "retryAttempt": 0,
    "retryAfterSeconds": None,
}
_install_lock = threading.Lock()
_checks_warm_lock = threading.Lock()
_checks_warming = False
_UPDATE_REPOSITORY = "manhgdev/zm_ai_tool"
_UPDATE_LOCK = threading.Lock()
_UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
_UPDATE_CANCEL = threading.Event()
_UPDATE_STATE: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "progress": 0,
    "message": "",
    "error": "",
    "assetName": "",
    "latestVersion": "",
    "packagePath": "",
    "packageSha256": "",
    "cancelRequested": False,
    "cancelledAt": None,
}


class _UpdateCancelled(Exception):
    pass


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
        f"https://api.github.com/repos/{_UPDATE_REPOSITORY}/releases?per_page=20",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ZM-AI-TOOL"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        return {}
    releases = [item for item in payload if isinstance(item, dict)
                and not item.get('draft') and not item.get('prerelease')
                and _version_key(str(item.get('tag_name') or '')) != (0, 0, 0)]
    return max(releases, key=lambda item: _version_key(str(item.get('tag_name') or '')), default={})




def _is_windows_installed_build() -> bool:
    """True khi app được cài bởi Inno Setup (có marker .zmaio-installed bên cạnh EXE)."""
    if not getattr(sys, "frozen", False):
        return False
    marker = Path(sys.executable).resolve().parent / ".zmaio-installed"
    return marker.is_file()


def _release_asset(release: dict[str, Any]) -> dict[str, Any] | None:
    tag = str(release.get("tag_name") or "")
    version = _release_version(tag)
    if _version_key(tag) == (0, 0, 0):
        return None
    assets = release.get("assets") or []
    if sys.platform == "win32":
        # Bản Installed → ưu tiên Setup.exe; bản Portable → ưu tiên Portable.zip
        if _is_windows_installed_build():
            candidates = [
                f"ZM_AI_TOOL_v{version}-windows-x64-Setup.exe",
                f"ZM_AI_TOOL_v{version}-windows-x64-Portable.zip",
                f"ZM_AI_TOOL_v{version}-windows-x64.zip",
            ]
        else:
            candidates = [
                f"ZM_AI_TOOL_v{version}-windows-x64-Portable.zip",
                f"ZM_AI_TOOL_v{version}-windows-x64.zip",
            ]
    elif sys.platform == "darwin":
        import platform as _platform
        machine = _platform.machine().lower()
        if machine in {"arm64", "aarch64"}:
            arch_tags = ("arm64",)
        elif machine in {"x86_64", "amd64"}:
            # CI/local pkg uses uname -m (x86_64); older builds used x64.
            arch_tags = ("x86_64", "x64")
        else:
            return None
        # Release ships .pkg only; keep .zip as fallback for older releases.
        candidates = []
        for arch in arch_tags:
            candidates.append(f"ZM_AI_TOOL_v{version}-macos-{arch}.pkg")
        for arch in arch_tags:
            candidates.append(f"ZM_AI_TOOL_v{version}-macos-{arch}.zip")
    else:
        return None
    for name in candidates:
        for asset in assets:
            if isinstance(asset, dict) and str(asset.get("name") or "") == name:
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
        if _UPDATE_CANCEL.is_set() and values.get('phase') not in {None, 'cancelled', 'cancelling'}:
            raise _UpdateCancelled
        _UPDATE_STATE.update(values)


def _update_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            if _UPDATE_CANCEL.is_set():
                raise _UpdateCancelled
            digest.update(chunk)
    return digest.hexdigest()


def _verify_update(path: Path, expected: str) -> None:
    if not re.fullmatch(r'[0-9a-f]{64}', expected) or _update_sha256(path) != expected:
        raise RuntimeError('UPDATE_CHECKSUM_MISMATCH: Update package integrity check failed')


def _download_update_parallel(url: str, partial: Path, expected_size: int) -> bool:
    """Download a large GitHub asset with four independent HTTP ranges."""
    if expected_size < 32 * 1024 * 1024 or partial.exists():
        return False
    workers = 4
    chunk_size = (expected_size + workers - 1) // workers
    parts = [partial.with_name(f"{partial.name}.{i}") for i in range(workers)]
    def fetch(index: int) -> Path:
        start = index * chunk_size
        end = min(expected_size - 1, start + chunk_size - 1)
        req = urllib.request.Request(url, headers={"User-Agent": "ZM-AI-TOOL", "Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(req, timeout=_UPDATE_DOWNLOAD_TIMEOUT_SECONDS) as response:
            if response.status != 206:
                raise RuntimeError("UPDATE_RANGE_UNSUPPORTED")
            target = parts[index]
            with target.open("wb") as stream:
                while block := response.read(8 * 1024 * 1024):
                    if _UPDATE_CANCEL.is_set():
                        raise _UpdateCancelled
                    stream.write(block)
        if target.stat().st_size != end - start + 1:
            raise OSError("UPDATE_RANGE_INCOMPLETE")
        return target
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="update-range") as pool:
            futures = [pool.submit(fetch, i) for i in range(workers)]
            completed = 0
            for future in as_completed(futures):
                future.result()
                completed += 1
                _set_update_state(progress=min(99, completed * 25))
        with partial.open("wb") as output:
            for part in parts:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output, length=8 * 1024 * 1024)
        return True
    finally:
        for part in parts:
            part.unlink(missing_ok=True)


def _download_update(asset: dict[str, Any], updates: Path, version: str) -> Path:
    name = str(asset.get("name") or "")
    url = str(asset.get("browser_download_url") or "")
    if not name or not url:
        raise RuntimeError("Release không có gói cài đặt phù hợp")
    # Asset names are supplied by GitHub; never allow a release response to
    # escape the dedicated update directory through a path separator.
    if Path(name).name != name or name in {".", ".."}:
        raise RuntimeError("Tên gói cập nhật không hợp lệ")
    expected_sha = str(asset.get('digest') or '').removeprefix('sha256:').lower()
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha):
        raise RuntimeError('UPDATE_DIGEST_MISSING: Release asset has no SHA-256; automatic execution is disabled')
    _set_update_state(packageSha256=expected_sha)
    try:
        expected_size = max(0, int(asset.get("size") or 0))
    except (TypeError, ValueError):
        expected_size = 0
    target = updates / name
    partial = target.with_suffix(target.suffix + ".part")
    # File đã tải đủ từ lần trước → dùng lại, không tải lại.
    if target.is_file() and expected_size and target.stat().st_size == expected_size:
        if _UPDATE_CANCEL.is_set():
            raise _UpdateCancelled
        _verify_update(target, expected_sha)
        if _UPDATE_CANCEL.is_set():
            raise _UpdateCancelled
        _set_update_state(phase="ready", progress=100, message="Đã tải gói cập nhật", assetName=name, latestVersion=version, packagePath=str(target))
        return target
    _set_update_state(phase="downloading", progress=0, message="Đang tải bản cập nhật…", assetName=name, latestVersion=version)
    try:
        parallel_done = _download_update_parallel(url, partial, expected_size)
    except _UpdateCancelled:
        raise
    except Exception:
        parallel_done = False
    if parallel_done:
        partial.replace(target)
        _verify_update(target, expected_sha)
        _set_update_state(progress=100)
        return target
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            if _UPDATE_CANCEL.is_set():
                raise _UpdateCancelled
            offset = partial.stat().st_size if partial.is_file() else 0
            request = urllib.request.Request(url, headers={"User-Agent": "ZM-AI-TOOL"})
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
                    next_progress_at = 0.0
                    while chunk := response.read(8 * 1024 * 1024):
                        if _UPDATE_CANCEL.is_set():
                            raise _UpdateCancelled
                        output.write(chunk)
                        received += len(chunk)
                        progress = min(99, int(received * 100 / total)) if total else 0
                        now = time.monotonic()
                        if now >= next_progress_at:
                            _set_update_state(progress=progress)
                            next_progress_at = now + 0.25
            partial.replace(target)
            actual_size = target.stat().st_size
            if expected_size and actual_size != expected_size:
                target.unlink(missing_ok=True)
                raise OSError(
                    f"Gói cập nhật tải chưa đủ ({actual_size}/{expected_size} byte)"
                )
            _verify_update(target, expected_sha)
            last_error = None
            break
        except _UpdateCancelled:
            raise
        except (BrokenPipeError, ConnectionError, TimeoutError, OSError) as exc:
            if _UPDATE_CANCEL.is_set():
                raise _UpdateCancelled from exc
            last_error = exc
            if attempt < 2:
                if expected_size and partial.is_file() and partial.stat().st_size > expected_size:
                    partial.unlink(missing_ok=True)
                _set_update_state(message=f"Kết nối gián đoạn, đang thử lại ({attempt + 2}/3)…")
                if _UPDATE_CANCEL.wait(0.5 * (attempt + 1)):
                    raise _UpdateCancelled
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
        """# ZM AI TOOL Windows Auto-Updater (PowerShell 5.1+ compatible)
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
$StartedFile = [string]$p.StartedFile

$LogFile = Join-Path (Split-Path $Zip -Parent) 'update.log'

function Log($msg) {
    $time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$time] $msg" | Out-File -FilePath $LogFile -Append -Encoding utf8
}

Log "=== Bat dau cap nhat ZM AI TOOL portable ==="
Log "AppPid:    $AppPid"
Log "Zip:       $Zip"
Log "Target:    $Target"
Log "Exe:       $Exe"
Log "ReadyFile: $ReadyFile"

try {
    if (-not (Test-Path -LiteralPath $Zip -PathType Leaf)) { throw 'Update ZIP missing' }
    if ((Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $p.PackageSHA256) { throw 'UPDATE_CHECKSUM_MISMATCH' }
    Log "Updater PID=$PID parent=$AppPid package SHA256=$($p.PackageSHA256)"
    if (-not $StartedFile) { throw 'Updater handshake path missing' }
    Set-Content -LiteralPath $StartedFile -Value 'ready' -Encoding ASCII
    $commitFile = $StartedFile + '.commit'
    $handshakeDeadline = (Get-Date).AddSeconds(40)
    while (-not (Test-Path -LiteralPath $commitFile -PathType Leaf)) {
        if ((Get-Date) -gt $handshakeDeadline) { Log 'App did not authorize handoff; stopping updater'; exit 1 }
        Start-Sleep -Milliseconds 100
    }
    Remove-Item -LiteralPath $commitFile -Force -ErrorAction SilentlyContinue
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

    # Keep Windows security metadata intact; never unblock files automatically.

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
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    Log "New EXE SHA256=$((Get-FileHash -LiteralPath $newExe -Algorithm SHA256).Hash)"
    $newProcess = Start-Process -FilePath $newExe -WorkingDirectory $Target -PassThru
    Log "Relaunch PID=$($newProcess.Id)"
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
        [System.Windows.Forms.MessageBox]::Show("Cap nhat that bai / Update failed:`n$err`n`nLog: $LogFile", "Loi cap nhat / Update error - ZM AI TOOL", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
    } catch {}
}
""",
        encoding="utf-8-sig",
    )
    return script


def _windows_setup_update_script(updates: Path) -> Path:
    script = updates / "apply-setup-update.ps1"
    script.write_text(
        """# ZM AI TOOL Windows installed updater
param([string]$ParamsFile)

$ErrorActionPreference = 'Stop'
$paramsPath = if ($ParamsFile) { $ParamsFile } else { Join-Path $PSScriptRoot 'setup-update-params.json' }
$p       = Get-Content -LiteralPath $paramsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$AppPid  = [int]$p.AppPid
$Setup   = [string]$p.Setup
$Target  = [string]$p.Target
$Exe     = [string]$p.Exe
$OldExe  = [string]$p.OldExe
$ReadyFile = [string]$p.ReadyFile
$StartedFile = [string]$p.StartedFile
$LogFile = Join-Path (Split-Path $Setup -Parent) 'setup-update.log'

function Log($msg) {
    $time = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "[$time] $msg" | Out-File -FilePath $LogFile -Append -Encoding utf8
}

try {
    Log '=== Bat dau cap nhat ZM AI TOOL installed ==='
    if (-not (Test-Path -LiteralPath $Setup -PathType Leaf)) { throw 'Update Setup missing' }
    if ((Get-FileHash -LiteralPath $Setup -Algorithm SHA256).Hash.ToLowerInvariant() -ne $p.PackageSHA256) { throw 'UPDATE_CHECKSUM_MISMATCH' }
    Log "Updater PID=$PID parent=$AppPid package SHA256=$($p.PackageSHA256)"
    if (Test-Path -LiteralPath $OldExe) { Log "Old EXE SHA256=$((Get-FileHash -LiteralPath $OldExe -Algorithm SHA256).Hash)" }
    if (-not $StartedFile) { throw 'Updater handshake path missing' }
    Set-Content -LiteralPath $StartedFile -Value 'ready' -Encoding ASCII
    $commitFile = $StartedFile + '.commit'
    $handshakeDeadline = (Get-Date).AddSeconds(40)
    while (-not (Test-Path -LiteralPath $commitFile -PathType Leaf)) {
        if ((Get-Date) -gt $handshakeDeadline) { Log 'App did not authorize handoff; stopping updater'; exit 1 }
        Start-Sleep -Milliseconds 100
    }
    Remove-Item -LiteralPath $commitFile -Force -ErrorAction SilentlyContinue
    if ($AppPid -gt 0) {
        Wait-Process -Id $AppPid -Timeout 20 -ErrorAction SilentlyContinue
    }

    # Setup is built with PrivilegesRequired=lowest and installs below
    # LocalAppData. Updates must never request elevation.
    $setupArgs = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="' + (Join-Path $PSScriptRoot 'setup-install.log') + '" /DIR="' + $Target + '"'
    $process = Start-Process -FilePath $Setup -ArgumentList $setupArgs -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Setup that bai voi ma $($process.ExitCode)."
    }

    $newExe = Join-Path $Target $Exe
    if (-not (Test-Path -LiteralPath $newExe -PathType Leaf)) {
        throw "Khong tim thay app sau cap nhat: $newExe"
    }
    Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue
    $env:ZM_AI_TOOL_UPDATE_READY_FILE = $ReadyFile
    $env:ZM_AI_TOOL_SUPERVISOR_CHILD = $null
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    Log "New EXE SHA256=$((Get-FileHash -LiteralPath $newExe -Algorithm SHA256).Hash)"
    $newProcess = Start-Process -FilePath $newExe -WorkingDirectory $Target -PassThru
    Log "Relaunch PID=$($newProcess.Id)"
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $ReadyFile -PathType Leaf)) {
        if ($newProcess.HasExited) {
            throw "Ban moi thoat som voi ma $($newProcess.ExitCode)."
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Path -LiteralPath $ReadyFile -PathType Leaf)) {
        Stop-Process -Id $newProcess.Id -Force -ErrorAction SilentlyContinue
        throw 'Ban moi khong bao san sang sau 90 giay.'
    }
    $env:ZM_AI_TOOL_UPDATE_READY_FILE = $null
    Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue
    Log "Da mo lai app: $newExe"
    Remove-Item -LiteralPath $Setup -Force -ErrorAction SilentlyContinue
} catch {
    $err = $_.Exception.Message
    Log "LOI CAP NHAT: $err"
    $env:ZM_AI_TOOL_UPDATE_READY_FILE = $null
    if ($ReadyFile) { Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue }
    if ($OldExe -and (Test-Path -LiteralPath $OldExe -PathType Leaf)) {
        Start-Process -FilePath $OldExe -WorkingDirectory (Split-Path $OldExe -Parent) -ErrorAction SilentlyContinue
        Log 'Da mo lai ban cu.'
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show("Cap nhat that bai / Update failed:`n$err`n`nLog: $LogFile", "Loi cap nhat / Update error - ZM AI TOOL", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
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


def _set_install_progress(
    progress: int | float,
    message: str = "",
    fields: dict[str, Any] | None = None,
) -> None:
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
        for key in (
            "stage", "runtimeProfile", "currentPackage", "runtimePack", "downloadedBytes", "totalBytes",
            "requiredDiskBytes", "diagnostics", "speedBytesPerSecond", "etaSeconds", "cacheHit", "downloadItems", "freeDiskBytes", "diskDrive", "retryAttempt", "retryAfterSeconds",
        ):
            if fields and key in fields:
                _install_state[key] = fields[key]
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
            if kind == 'rollback' or _install_state.get('kind') == 'rollback':
                raise HTTPException(status_code=409, detail='RUNTIME_BUSY')
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
            stage="detect_hardware" if kind in ("ai_runtime", "ocr_cuda") else "",
            runtimePack="",
            runtimeProfile="",
            currentPackage="",
            speedBytesPerSecond=None, etaSeconds=None, cacheHit=0, downloadItems=[],
            freeDiskBytes=None, diskDrive="", retryAttempt=0, retryAfterSeconds=None,
            downloadedBytes=0,
            totalBytes=0,
            requiredDiskBytes=0,
            errorCode="",
            retryable=False,
            diagnostics="",
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
                for key in ("runtimePack", "runtimeProfile", "diagnostics"):
                    if key in result:
                        _install_state[key] = result[key]
                _install_state["updatedAt"] = time.time()
        except Exception as e:
            from pipeline.core.runtime_install import RuntimeInstallError

            with _install_lock:
                _install_state["error"] = str(e)
                if isinstance(e, RuntimeInstallError):
                    _install_state["errorCode"] = e.code
                    _install_state["retryable"] = e.retryable
                    _install_state["diagnostics"] = e.diagnostics
                elif kind in ("ai_runtime", "ocr_cuda", "demucs_cuda"):
                    _install_state["errorCode"] = "RUNTIME_PROBE_FAILED"
                    _install_state["retryable"] = True
                    _install_state["diagnostics"] = repr(e)
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
    for key in (
        "stage", "runtimeProfile", "currentPackage", "runtimePack", "downloadedBytes", "totalBytes",
        "requiredDiskBytes", "errorCode", "retryable", "diagnostics", "speedBytesPerSecond", "etaSeconds", "cacheHit", "downloadItems", "freeDiskBytes", "diskDrive", "retryAttempt", "retryAfterSeconds",
    ):
        out[key] = st.get(key)
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


@router.post("/api/system/install/rollback")
def api_install_rollback():
    from pipeline.core.runtime_install import rollback_runtime

    return _start_install_job("rollback", lambda: rollback_runtime(_set_install_progress))


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
        if _UPDATE_STATE["running"] or _UPDATE_STATE['phase'] == 'applying':
            return {"ok": True, "running": True, "message": _UPDATE_STATE["message"]}
        _UPDATE_CANCEL.clear()
        _UPDATE_STATE.update(running=True, phase="checking", progress=0, message="Đang chuẩn bị cập nhật…", error="", packagePath="", cancelRequested=False, cancelledAt=None)

    def work() -> None:
        try:
            release = _latest_release()
            tag = str(release.get("tag_name") or "")
            version = _release_version(tag)
            asset = _release_asset(release)
            if not asset or _version_key(tag) <= _version_key(_desktop_version()):
                _set_update_state(phase="complete", progress=100, message="Đã là phiên bản mới nhất")
                return
            # Bản Portable cập nhật bằng cách giải nén đè vào exe_dir → cần kiểm tra quyền ghi.
            # Bản Installed tải Setup.exe vào LocalAppData/updates → không cần probe exe_dir.
            if sys.platform == "win32" and getattr(sys, "frozen", False) and not _is_windows_installed_build():
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
                        "Portable sang nơi có quyền ghi (ví dụ C:\\ZM_AI_TOOL) rồi thử lại. / "
                        "The app folder is not writable. Move the complete Portable folder to a "
                        "writable location (for example C:\\ZM_AI_TOOL) and retry."
                    ) from exc
            updates = Path(os.environ.get("ZM_AI_TOOL_HOME") or DATA) / "updates"
            updates.mkdir(parents=True, exist_ok=True)
            package = _download_update(asset, updates, version)
            if _UPDATE_CANCEL.is_set():
                raise _UpdateCancelled
            _set_update_state(phase="ready", progress=100, message="Đã tải gói cập nhật", packagePath=str(package))
        except _UpdateCancelled:
            _set_update_state(running=False, phase="cancelled", message="Đã hủy cập nhật", cancelRequested=True, cancelledAt=time.time())
        except Exception as exc:
            _set_update_state(phase="error", error=str(exc), message="Không thể tải bản cập nhật")
        finally:
            _set_update_state(running=False)

    threading.Thread(target=work, name="desktop-update-download", daemon=True).start()
    return {"ok": True, "running": True, "message": "Đang tải bản cập nhật…"}


@router.post("/api/system/update/cancel")
def api_update_cancel():
    with _UPDATE_LOCK:
        state = dict(_UPDATE_STATE)
        if state["phase"] == "applying":
            raise HTTPException(409, "Không thể hủy sau khi updater đã nhận gói")
        if not state["running"] and state["phase"] not in {"ready", "downloading", "checking"}:
            return {"ok": True, "cancelled": state["phase"] == "cancelled"}
        _UPDATE_CANCEL.set()
        _UPDATE_STATE.update(phase="cancelling" if state['running'] else 'cancelled', message="Đang hủy cập nhật…", cancelRequested=True, cancelledAt=time.time())
    return {"ok": True, "cancelling": True}


@router.get("/api/system/update/status")
def api_update_status():
    desktop = os.environ.get("ZM_AI_TOOL_DESKTOP")
    return {"desktop": desktop == "1", **_update_snapshot()}


def _windows_user_install_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "Programs" / "ZM AI TOOL"


def _macos_app_bundle() -> Path:
    executable = Path(sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix.lower() == ".app":
            return parent
    return Path.home() / "Applications" / "ZM AI TOOL.app"


def _macos_update_script(updates: Path) -> Path:
    script = updates / "apply-macos-update.sh"
    script.write_text(
        """#!/bin/bash
set -u

APP_PID="$1"
PACKAGE="$2"
TARGET_APP="$3"
LOG_FILE="$(/usr/bin/dirname "$PACKAGE")/macos-update.log"
TMP_DIR="$(/usr/bin/dirname "$PACKAGE")/macos-extracted-$(/bin/date +%Y%m%d%H%M%S)-$$"

log() {
  /bin/echo "[$(/bin/date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

show_error() {
  /usr/bin/osascript -e "display dialog \"Cập nhật thất bại / Update failed:\\n$1\\n\\nLog: $LOG_FILE\" with title \"ZM AI TOOL\" buttons {\"OK\"} default button \"OK\"" >/dev/null 2>&1 || true
}

launch_and_wait() {
  app="$1"
  executable="$app/Contents/MacOS/ZM AI TOOL"
  /usr/bin/open -n "$app" >/dev/null 2>&1 || return 1
  attempts=0
  while [ "$attempts" -lt 90 ]; do
    /usr/bin/pgrep -f -- "$executable" >/dev/null 2>&1 && return 0
    /bin/sleep 1
    attempts=$((attempts + 1))
  done
  return 1
}

restore_backup() {
  destination="$1"
  backup="$2"
  parent="$(/usr/bin/dirname "$destination")"
  if [ -w "$parent" ]; then
    /bin/rm -rf "$destination"
    /bin/mv "$backup" "$destination" || return 1
  elif [ -w "$destination" ]; then
    /bin/rm -rf "$destination/Contents"
    /usr/bin/ditto "$backup/Contents" "$destination/Contents" || return 1
    /bin/rm -rf "$backup"
  else
    return 1
  fi
}

install_app() {
  destination="$1"
  parent="$(/usr/bin/dirname "$destination")"
  name="$(/usr/bin/basename "$destination")"
  backup="$(/usr/bin/dirname "$PACKAGE")/$name.backup.$(/bin/date +%s).$$"

  /bin/mkdir -p "$parent" 2>/dev/null || return 1
  /bin/rm -rf "$backup"
  if [ -d "$destination" ]; then
    /usr/bin/ditto "$destination" "$backup" || return 1
  fi

  if [ -d "$destination" ] && [ -w "$destination" ]; then
    /bin/rm -rf "$destination/Contents" || return 1
    /usr/bin/ditto "$SOURCE_APP/Contents" "$destination/Contents" || {
      restore_backup "$destination" "$backup" || true
      return 1
    }
  elif [ -w "$parent" ]; then
    staged="$parent/.$name.updating.$$"
    /bin/rm -rf "$staged"
    /usr/bin/ditto "$SOURCE_APP" "$staged" || return 1
    /bin/rm -rf "$destination"
    /bin/mv "$staged" "$destination" || {
      restore_backup "$destination" "$backup" || true
      return 1
    }
  else
    /bin/rm -rf "$backup"
    return 1
  fi

  if launch_and_wait "$destination"; then
    /bin/rm -rf "$backup"
    return 0
  fi

  log "Ban moi khong khoi dong; dang khoi phuc $destination"
  restore_backup "$destination" "$backup" || true
  [ -d "$destination" ] && /usr/bin/open -n "$destination" >/dev/null 2>&1 || true
  return 1
}

case "$TARGET_APP" in
  *.app) ;;
  *) show_error "Đường dẫn ứng dụng không hợp lệ."; exit 1 ;;
esac

log "=== Bat dau cap nhat macOS ZM AI TOOL ==="
if [ "$APP_PID" -gt 0 ] 2>/dev/null; then
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    /bin/kill -0 "$APP_PID" >/dev/null 2>&1 || break
    /bin/sleep 1
  done
fi

/bin/rm -rf "$TMP_DIR"
/bin/mkdir -p "$TMP_DIR" || { show_error "Không tạo được thư mục tạm."; exit 1; }

case "$PACKAGE" in
  *.pkg)
    EXPAND_DIR="$TMP_DIR/expanded"
    if /usr/sbin/pkgutil --expand-full "$PACKAGE" "$EXPAND_DIR" >/dev/null 2>&1; then
      :
    elif /usr/sbin/pkgutil --expand "$PACKAGE" "$EXPAND_DIR" >/dev/null 2>&1; then
      PAYLOAD="$(/usr/bin/find "$EXPAND_DIR" -name Payload -print -quit)"
      if [ -n "$PAYLOAD" ]; then
        /bin/mkdir -p "$EXPAND_DIR/PayloadRoot"
        (cd "$EXPAND_DIR/PayloadRoot" && /usr/bin/tar -xvf "$PAYLOAD") >/dev/null 2>&1 \
          || (cd "$EXPAND_DIR/PayloadRoot" && /usr/bin/ditto -x "$PAYLOAD" .) >/dev/null 2>&1 \
          || { show_error "Không giải nén được Payload trong gói .pkg."; exit 1; }
      fi
    else
      show_error "Không giải nén được gói .pkg."
      exit 1
    fi
    SOURCE_APP="$(/usr/bin/find "$EXPAND_DIR" -maxdepth 6 -type d -name '*.app' -print -quit)"
    ;;
  *.zip)
    /usr/bin/ditto -x -k "$PACKAGE" "$TMP_DIR" || { show_error "Không giải nén được gói cập nhật."; exit 1; }
    SOURCE_APP="$(/usr/bin/find "$TMP_DIR" -maxdepth 3 -type d -name '*.app' -print -quit)"
    ;;
  *)
    show_error "Định dạng gói cập nhật không hỗ trợ."
    exit 1
    ;;
esac

if [ -z "$SOURCE_APP" ] || [ ! -x "$SOURCE_APP/Contents/MacOS/ZM AI TOOL" ]; then
  show_error "Gói cập nhật không hợp lệ."
  exit 1
fi

if install_app "$TARGET_APP"; then
  log "Cap nhat thanh cong vao $TARGET_APP"
  /bin/rm -rf "$TMP_DIR" "$PACKAGE"
  exit 0
fi

USER_APP="$HOME/Applications/ZM AI TOOL.app"
if [ "$TARGET_APP" != "$USER_APP" ] && install_app "$USER_APP"; then
  log "Da chuyen ban moi sang $USER_APP vi target cu khong cho ghi"
  /usr/bin/osascript -e 'display notification "Đã mở bản mới từ thư mục Applications của tài khoản." with title "ZM AI TOOL"' >/dev/null 2>&1 || true
  /bin/rm -rf "$TMP_DIR" "$PACKAGE"
  exit 0
fi

show_error "Không thể ghi bản cập nhật hoặc bản mới không khởi động được."
[ -d "$TARGET_APP" ] && /usr/bin/open -n "$TARGET_APP" >/dev/null 2>&1 || true
exit 1
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _launch_macos_updater(package: Path) -> None:
    script = _macos_update_script(package.parent)
    subprocess.Popen(
        [
            "/bin/bash",
            str(script),
            str(os.getpid()),
            str(package.resolve()),
            str(_macos_app_bundle()),
        ],
        cwd=str(package.parent),
        close_fds=True,
        start_new_session=True,
    )


def _spawn_windows_updater(command: list[str], *, started: Path, log_path: Path, **kwargs) -> None:
    """Do not close the app until the detached updater acknowledges startup."""
    started.unlink(missing_ok=True)
    commit = Path(str(started) + '.commit')
    commit.unlink(missing_ok=True)
    from pipeline.core.runtime_site import subprocess_environment
    env = subprocess_environment()
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    env.pop('ZM_AI_TOOL_SUPERVISOR_CHILD', None)
    with log_path.open('wb') as log:
        log.write((subprocess.list2cmdline(command) + '\n').encode('utf-8'))
        log.flush()
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=env, close_fds=True, **kwargs)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if started.is_file():
            commit.write_text('proceed', encoding='ascii')
            started.unlink(missing_ok=True)
            return
        time.sleep(0.1)
    exit_code = process.poll()
    if exit_code is None:
        process.terminate()
    detail = log_path.read_text(encoding='utf-8', errors='replace')[-4000:]
    reason = f'exit code {exit_code}' if exit_code is not None else 'startup timed out after 30s'
    raise RuntimeError(f'Updater did not acknowledge startup ({reason}). App kept open. Log: {log_path}\n{detail}')


def _launch_windows_updater(package: Path) -> None:
    """Start the detached staged updater; it waits for this app before swapping."""
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    package_sha = _update_snapshot().get('packageSha256') or ''
    _verify_update(package, str(package_sha))

    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file():
        raise RuntimeError(f"Windows PowerShell is missing: {powershell}")

    if package.suffix.lower() == ".exe":
        exe = Path(sys.executable).resolve()
        target = exe.parent if (exe.parent / ".zmaio-installed").is_file() else _windows_user_install_dir()
        script = _windows_setup_update_script(package.parent)
        params = package.parent / "setup-update-params.json"
        ready = package.parent / f"setup-update-ready-{uuid.uuid4().hex}.txt"
        ready.unlink(missing_ok=True)
        params.write_text(
            json.dumps(
                {
                    "AppPid": os.getpid(),
                    "PackageSHA256": package_sha,
                    "Setup": str(package.resolve()),
                    "Target": str(target.resolve()),
                    "Exe": exe.name,
                    "OldExe": str(exe),
                    "ReadyFile": str(ready.resolve()),
                    "StartedFile": str(ready.with_suffix(".started").resolve()),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _spawn_windows_updater(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "RemoteSigned",
                "-File",
                str(script),
                "-ParamsFile",
                str(params),
            ],
            cwd=str(package.parent),
            creationflags=flags,
            started=ready.with_suffix('.started'),
            log_path=package.parent / 'updater-bootstrap.log',
        )
        return

    # Bản Portable: zip extractor script.
    exe = Path(sys.executable).resolve()
    target = exe.parent
    script = _windows_update_script(package.parent)
    params = package.parent / "update-params.json"
    ready = package.parent / f"update-ready-{uuid.uuid4().hex}.txt"
    ready.unlink(missing_ok=True)
    params.write_text(
        json.dumps(
            {
                "AppPid": os.getpid(),
                "PackageSHA256": package_sha,
                "Zip": str(package.resolve()),
                "Target": str(target.resolve()),
                "Exe": exe.name,
                "ReadyFile": str(ready.resolve()),
                    "StartedFile": str(ready.with_suffix(".started").resolve()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _spawn_windows_updater(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
                "RemoteSigned",
            "-File",
            str(script),
            "-ParamsFile",
            str(params),
        ],
        cwd=str(package.parent),
        creationflags=flags,
        started=ready.with_suffix('.started'),
        log_path=package.parent / 'updater-bootstrap.log',
    )


@router.post("/api/system/update/apply")
def api_update_apply():
    if not _update_supported():
        raise HTTPException(400, "Chỉ bản desktop hỗ trợ cập nhật")
    state = _update_snapshot()
    if state.get("cancelRequested") or _UPDATE_CANCEL.is_set() or state["phase"] == "cancelled":
        raise HTTPException(409, "UPDATE_CANCELLED")
    if state["running"]:
        raise HTTPException(409, "Gói cập nhật vẫn đang tải")
    if state["phase"] != "ready" or not state["packagePath"]:
        raise HTTPException(400, "Chưa có gói cập nhật đã tải")
    package = Path(str(state["packagePath"]))
    if not package.is_file():
        raise HTTPException(404, "Không tìm thấy gói cập nhật đã tải")
    updates = (Path(os.environ.get('ZM_AI_TOOL_HOME') or DATA) / 'updates').resolve()
    if package.resolve().parent != updates or package.is_symlink():
        raise HTTPException(400, 'UPDATE_UNSAFE_PATH')
    try:
        _verify_update(package, str(state.get('packageSha256') or ''))
    except (OSError, RuntimeError) as exc:
        _set_update_state(phase='error', error=str(exc))
        raise HTTPException(400, str(exc)) from exc
    except _UpdateCancelled as exc:
        raise HTTPException(409, 'UPDATE_CANCELLED') from exc
    # Atomic handoff boundary: cancellation and concurrent apply cannot both win.
    with _UPDATE_LOCK:
        if _UPDATE_CANCEL.is_set() or _UPDATE_STATE['phase'] != 'ready':
            raise HTTPException(409, 'UPDATE_CANCELLED_OR_BUSY')
        _UPDATE_STATE.update(phase='applying')
    if sys.platform == "darwin":
        if package.suffix.lower() not in {".pkg", ".zip"}:
            raise HTTPException(400, "Gói cập nhật macOS không hợp lệ")
        try:
            _launch_macos_updater(package)
        except Exception as exc:
            _set_update_state(phase="error", error=str(exc), message="Không thể mở trình cập nhật")
            raise HTTPException(500, f"Không thể mở trình cập nhật: {exc}") from exc
        _set_update_state(phase="applying", progress=100, message="Đang đóng app để cập nhật và mở lại…")
        threading.Timer(0.8, lambda: os._exit(0)).start()
        return {"ok": True, "message": "Đang đóng app để cập nhật và mở lại…"}

    if sys.platform != "win32":
        raise HTTPException(400, "Hệ điều hành này chưa hỗ trợ cập nhật tự động")

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
$dialog.Description = $env:ZM_AIO_DIALOG_TITLE
$dialog.ShowNewFolderButton = $true
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.SelectedPath)
}
$owner.Close()
""",
            {"ZM_AIO_DIALOG_TITLE": title},
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
$dialog.Title = $env:ZM_AIO_DIALOG_TITLE
$dialog.Filter = $env:ZM_AIO_FILE_FILTER
$dialog.Multiselect = $false
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.FileName)
}
$owner.Close()
""",
            {
                "ZM_AIO_DIALOG_TITLE": title,
                "ZM_AIO_FILE_FILTER": file_filter,
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
$dialog.Title = $env:ZM_AIO_DIALOG_TITLE
$dialog.Filter = $env:ZM_AIO_FILE_FILTER
$dialog.Multiselect = $true
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write(($dialog.FileNames -join [Environment]::NewLine))
}
$owner.Close()
""",
                {"ZM_AIO_DIALOG_TITLE": title, "ZM_AIO_FILE_FILTER": filt},
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
$dialog.FileName = $env:ZM_AIO_SAVE_NAME
$dialog.InitialDirectory = $env:ZM_AIO_SAVE_DIR
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.FileName)
}
$owner.Close()
""",
                {"ZM_AIO_SAVE_NAME": initial, "ZM_AIO_SAVE_DIR": str(initial_dir)},
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
